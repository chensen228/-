from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from graph_backend import CampusGraphService

try:
    import redis as redis_lib
except ImportError:
    redis_lib = None

try:
    from mongo_real_backend import RealMongoStore
except ImportError:
    RealMongoStore = None


APP_DIR = Path(__file__).resolve().parent
CURRENT_TERM = "2025-2026-2"
RUNNING_ON_RENDER = os.getenv("RENDER", "").lower() == "true"
FORCE_LOCAL_FALLBACK = os.getenv("SMART_CAMPUS_FORCE_FALLBACK", "").lower() in {"1", "true", "yes", "on"}
DEFAULT_RUNTIME_DATA_DIR = APP_DIR / "data"
if RUNNING_ON_RENDER:
    DEFAULT_RUNTIME_DATA_DIR = Path(tempfile.gettempdir()) / "smart_campus_demo_data"
DATA_DIR = Path(os.getenv("SMART_CAMPUS_RUNTIME_DIR", str(DEFAULT_RUNTIME_DATA_DIR)))
MONGO_DIR = DATA_DIR / "mongo"
DB_PATH = DATA_DIR / "smart_campus.db"
REDIS_STATE_PATH = DATA_DIR / "redis_state.json"
REDIS_HOST = os.getenv("SMART_CAMPUS_REDIS_HOST", "" if RUNNING_ON_RENDER else "127.0.0.1")
REDIS_PORT = int(os.getenv("SMART_CAMPUS_REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("SMART_CAMPUS_REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("SMART_CAMPUS_REDIS_PASSWORD", "" if RUNNING_ON_RENDER else "123456")
REDIS_NAMESPACE = os.getenv("SMART_CAMPUS_REDIS_NAMESPACE", "smartcampus:")
MONGO_URI = os.getenv("SMART_CAMPUS_MONGO_URI", "" if RUNNING_ON_RENDER else "mongodb://127.0.0.1:27017")
MONGO_DB_NAME = os.getenv("SMART_CAMPUS_MONGO_DB_NAME", "smart_campus")
JSON_MARKER = "__json__:"
BOOK_RANK_TTLS = {"day": 24 * 3600, "week": 7 * 24 * 3600, "month": 30 * 24 * 3600, "category_day": 24 * 3600}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_time_text(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


class RedisLite:
    mode = "fallback"
    status_label = "模拟 Redis（本地 JSON 回退）"

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self._save(self._default_state())

    def _default_state(self) -> dict[str, Any]:
        return {"strings": {}, "zsets": {}, "expiry": {}}

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._default_state()
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        for key in ["strings", "zsets", "expiry"]:
            data.setdefault(key, {})
        self._cleanup(data)
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _cleanup(self, data: dict[str, Any]) -> None:
        now = time.time()
        expired_keys = [key for key, expire_at in data["expiry"].items() if expire_at <= now]
        for key in expired_keys:
            data["strings"].pop(key, None)
            data["zsets"].pop(key, None)
            data["expiry"].pop(key, None)

    def reset(self) -> None:
        self._save(self._default_state())

    def get(self, key: str) -> Any:
        data = self._load()
        return data["strings"].get(key)

    def set(self, key: str, value: Any, ex: int | None = None) -> None:
        data = self._load()
        data["strings"][key] = value
        if ex:
            data["expiry"][key] = time.time() + ex
        else:
            data["expiry"].pop(key, None)
        self._save(data)

    def delete(self, key: str) -> None:
        data = self._load()
        data["strings"].pop(key, None)
        data["zsets"].pop(key, None)
        data["expiry"].pop(key, None)
        self._save(data)

    def incr(self, key: str, amount: int = 1) -> int:
        value = int(self.get(key) or 0) + amount
        self.set(key, value)
        return value

    def setnx(self, key: str, value: Any, ex: int | None = None) -> bool:
        data = self._load()
        if key in data["strings"]:
            return False
        data["strings"][key] = value
        if ex:
            data["expiry"][key] = time.time() + ex
        self._save(data)
        return True

    def zadd(self, key: str, member: str, amount: int, ex: int | None = None) -> None:
        data = self._load()
        zset = data["zsets"].setdefault(key, {})
        zset[member] = int(zset.get(member, 0)) + amount
        if ex is not None:
            data["expiry"][key] = time.time() + ex
        self._save(data)

    def ztop(self, key: str, limit: int = 5) -> list[tuple[str, int]]:
        data = self._load()
        zset = data["zsets"].get(key, {})
        return sorted(zset.items(), key=lambda item: item[1], reverse=True)[:limit]

    def summary(self, limit: int = 12) -> list[dict[str, Any]]:
        data = self._load()
        items: list[dict[str, Any]] = []
        for key, value in data["strings"].items():
            preview = value
            if isinstance(preview, (dict, list)):
                preview = json.dumps(preview, ensure_ascii=False)
            items.append({"key": key, "type": "String", "preview": str(preview)[:80]})
        for key, value in data["zsets"].items():
            ranking = sorted(value.items(), key=lambda item: item[1], reverse=True)
            preview = ", ".join(f"{member}:{score}" for member, score in ranking[:3])
            items.append({"key": key, "type": "ZSet", "preview": preview or "empty"})
        items.sort(key=lambda item: item["key"])
        return items[:limit]


class RealRedisStore:
    mode = "real"

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        db: int,
        namespace: str,
    ) -> None:
        if redis_lib is None:
            raise RuntimeError("redis package is not installed")
        self.host = host
        self.port = port
        self.password = password
        self.db = db
        self.namespace = namespace
        self.client = redis_lib.Redis(
            host=host,
            port=port,
            password=password,
            db=db,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        self.client.ping()
        self.status_label = f"真实 Redis（{host}:{port}/db{db}）"

    def _key(self, key: str) -> str:
        return f"{self.namespace}{key}"

    def _strip(self, key: str) -> str:
        return key[len(self.namespace) :] if key.startswith(self.namespace) else key

    def _encode(self, value: Any) -> str:
        return JSON_MARKER + json.dumps(value, ensure_ascii=False)

    def _decode(self, raw: str | None) -> Any:
        if raw is None:
            return None
        if raw.startswith(JSON_MARKER):
            return json.loads(raw[len(JSON_MARKER) :])
        return raw

    def reset(self) -> None:
        keys = list(self.client.scan_iter(match=f"{self.namespace}*"))
        if keys:
            self.client.delete(*keys)

    def get(self, key: str) -> Any:
        return self._decode(self.client.get(self._key(key)))

    def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.client.set(self._key(key), self._encode(value), ex=ex)

    def delete(self, key: str) -> None:
        self.client.delete(self._key(key))

    def incr(self, key: str, amount: int = 1) -> int:
        value = int(self.get(key) or 0) + amount
        self.set(key, value)
        return value

    def setnx(self, key: str, value: Any, ex: int | None = None) -> bool:
        return bool(self.client.set(self._key(key), self._encode(value), ex=ex, nx=True))

    def zadd(self, key: str, member: str, amount: int, ex: int | None = None) -> None:
        redis_key = self._key(key)
        pipeline = self.client.pipeline()
        pipeline.zincrby(redis_key, amount, member)
        if ex is not None:
            pipeline.expire(redis_key, ex)
        pipeline.execute()

    def ztop(self, key: str, limit: int = 5) -> list[tuple[str, int]]:
        rows = self.client.zrevrange(self._key(key), 0, max(limit - 1, 0), withscores=True)
        return [(member, int(score)) for member, score in rows]

    def summary(self, limit: int = 12) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for key in sorted(self.client.scan_iter(match=f"{self.namespace}*")):
            key_type = self.client.type(key)
            clean_key = self._strip(key)
            if key_type == "string":
                preview = self.get(clean_key)
                if isinstance(preview, (dict, list)):
                    preview = json.dumps(preview, ensure_ascii=False)
                items.append({"key": clean_key, "type": "String", "preview": str(preview)[:80]})
            elif key_type == "zset":
                ranking = self.client.zrevrange(key, 0, 2, withscores=True)
                preview = ", ".join(f"{member}:{int(score)}" for member, score in ranking)
                items.append({"key": clean_key, "type": "ZSet", "preview": preview or "empty"})
            if len(items) >= limit:
                break
        return items


class MongoLite:
    mode = "fallback"
    status_label = "模拟 Mongo（本地 JSON 文档回退）"

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, collection: str) -> Path:
        return self.base_dir / f"{collection}.json"

    def reset(self) -> None:
        for path in self.base_dir.glob("*.json"):
            path.unlink()

    def read_all(self, collection: str) -> list[dict[str, Any]]:
        path = self._path(collection)
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def write_all(self, collection: str, documents: list[dict[str, Any]]) -> None:
        self._path(collection).write_text(json.dumps(documents, ensure_ascii=False, indent=2), encoding="utf-8")

    def insert(self, collection: str, document: dict[str, Any]) -> None:
        documents = self.read_all(collection)
        document = dict(document)
        document["_id"] = f"{collection}-{len(documents) + 1:04d}"
        document["createdAt"] = document.get("createdAt", now_text())
        documents.append(document)
        self.write_all(collection, documents)

    def recent(self, collection: str, limit: int = 6) -> list[dict[str, Any]]:
        return list(reversed(self.read_all(collection)))[0:limit]

    def count(self, collection: str) -> int:
        return len(self.read_all(collection))

    def ensure_structures(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def insert_event_feed(self, event_type: str, message: str, context: dict[str, Any] | None = None) -> None:
        self.insert("realtime_event_feed", {"eventType": event_type, "message": message, "context": context or {}})

    def recent_event_feed(self, limit: int = 6) -> list[dict[str, Any]]:
        return self.recent("realtime_event_feed", limit=limit)

    def cache_search(self, keyword: str, source: str = "web", ttl_minutes: int = 90) -> None:
        self.insert(
            "search_session_cache",
            {
                "keyword": keyword,
                "source": source,
                "expireAt": (datetime.now() + timedelta(minutes=ttl_minutes)).strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    def search_cache_stats(self, limit: int = 4) -> dict[str, Any]:
        items = self.recent("search_session_cache", limit=limit)
        now = datetime.now()
        valid_items = [item for item in items if parse_time_text(item.get("expireAt")) > now]
        return {"count": len(valid_items), "recent": valid_items}

    def upsert_geo_spaces(self, spaces: list[dict[str, Any]]) -> None:
        payload = []
        for space in spaces:
            item = dict(space)
            item["updatedAt"] = now_text()
            payload.append(item)
        self.write_all("campus_space_geo", payload)

    def nearby_spaces(self, lng: float, lat: float, limit: int = 5, space_type: str | None = None) -> list[dict[str, Any]]:
        rows = self.read_all("campus_space_geo")
        if space_type:
            rows = [row for row in rows if row.get("spaceType") == space_type]
        for row in rows:
            coordinates = row.get("location", {}).get("coordinates", [0, 0])
            row["distanceScore"] = round(((coordinates[0] - lng) ** 2 + (coordinates[1] - lat) ** 2) ** 0.5, 6)
        rows.sort(key=lambda item: item.get("distanceScore", 999))
        return rows[:limit]

    def gridfs_sync_assets(self, asset_paths: list[Path]) -> None:
        manifest = []
        for path in asset_paths:
            if not path.exists():
                continue
            manifest.append(
                {
                    "_id": f"gridfs-{path.stem}",
                    "filename": path.name,
                    "length": path.stat().st_size,
                    "uploadDate": now_text(),
                    "metadata": {"sourcePath": str(path), "category": "smart-campus-demo"},
                }
            )
        self.write_all("gridfs_manifest", manifest)

    def gridfs_files(self, limit: int = 6) -> list[dict[str, Any]]:
        return self.recent("gridfs_manifest", limit=limit)

    def collection_catalog(self) -> list[dict[str, Any]]:
        names = [
            "library_behavior_log",
            "teaching_change_log",
            "warning_profile",
            "internship_weekly_report",
            "evaluation_comment",
            "practice_risk_profile",
            "realtime_event_feed",
            "search_session_cache",
            "campus_space_geo",
            "data_quality_snapshot",
        ]
        return [
            {
                "name": name,
                "count": self.count(name),
                "capped": name == "realtime_event_feed",
                "validator": "app-level fallback",
                "indexes": ["local-json-order"],
                "ttl_indexes": ["expireAt"] if name == "search_session_cache" else [],
            }
            for name in names
        ]

    def aggregation_showcase(self) -> dict[str, Any]:
        def summarize(collection: str, key: str) -> list[dict[str, Any]]:
            counter = Counter((item.get(key) or "unknown") for item in self.read_all(collection))
            return [{"name": name, "count": count} for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))]

        return {
            "library_actions": summarize("library_behavior_log", "action"),
            "space_types": summarize("campus_space_geo", "spaceType"),
            "event_types": summarize("realtime_event_feed", "eventType"),
            "pipelines": [
                {
                    "title": "借阅行为聚合",
                    "stages": "$group -> $sort",
                    "target": "library_behavior_log.action",
                },
                {
                    "title": "空间类型聚合",
                    "stages": "$group -> $sort",
                    "target": "campus_space_geo.spaceType",
                },
                {
                    "title": "事件流聚合",
                    "stages": "$group -> $sort",
                    "target": "realtime_event_feed.eventType",
                },
            ],
        }


class SmartCampusRepository:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.db_path = DB_PATH
        self._last_redis_probe = 0.0
        self._redis_probe_interval = 8.0
        self._last_mongo_probe = 0.0
        self._mongo_probe_interval = 8.0
        self._rank_bootstrap_checked = False
        self.redis = self._create_redis_store()
        if self.redis.mode == "fallback":
            self._last_redis_probe = time.time()
        self.mongo = self._create_mongo_store()
        if self.mongo.mode == "fallback":
            self._last_mongo_probe = time.time()
        self.graph = CampusGraphService(DATA_DIR / "graph")
        self.ensure_ready()

    def _create_redis_store(self) -> RedisLite | RealRedisStore:
        if FORCE_LOCAL_FALLBACK or not REDIS_HOST:
            return RedisLite(REDIS_STATE_PATH)
        if redis_lib is not None:
            try:
                return RealRedisStore(
                    host=REDIS_HOST,
                    port=REDIS_PORT,
                    password=REDIS_PASSWORD,
                    db=REDIS_DB,
                    namespace=REDIS_NAMESPACE,
                )
            except Exception:
                pass
        return RedisLite(REDIS_STATE_PATH)

    def _create_mongo_store(self) -> MongoLite | Any:
        if FORCE_LOCAL_FALLBACK or not MONGO_URI:
            return MongoLite(MONGO_DIR)
        if RealMongoStore is not None:
            try:
                return RealMongoStore(MONGO_URI, MONGO_DB_NAME)
            except Exception:
                pass
        return MongoLite(MONGO_DIR)

    def ensure_redis_ready(self) -> None:
        if self.redis.mode == "real":
            try:
                self.redis.client.ping()
                if self.db_path.exists() and not self.redis.summary(limit=1):
                    self._seed_redis()
                elif self.db_path.exists() and not self._rank_bootstrap_checked:
                    self._bootstrap_rank_indexes()
            except Exception:
                self.redis = RedisLite(REDIS_STATE_PATH)
                self._last_redis_probe = time.time()
                self._rank_bootstrap_checked = False
            return

        now = time.time()
        if now - self._last_redis_probe < self._redis_probe_interval:
            if self.db_path.exists() and not self._rank_bootstrap_checked:
                self._bootstrap_rank_indexes()
            return
        self._last_redis_probe = now
        preferred = self._create_redis_store()
        if preferred.mode == "real":
            self.redis = preferred
            self._rank_bootstrap_checked = False
            if self.db_path.exists() and not self.redis.summary(limit=1):
                self._seed_redis()
            elif self.db_path.exists() and not self._rank_bootstrap_checked:
                self._bootstrap_rank_indexes()
        elif self.db_path.exists() and not self._rank_bootstrap_checked:
            self._bootstrap_rank_indexes()

    def redis_runtime(self) -> dict[str, str]:
        self.ensure_redis_ready()
        host = REDIS_HOST or ("内置回退存储" if self.redis.mode != "real" else "")
        port = str(REDIS_PORT) if REDIS_HOST else "-"
        db = str(REDIS_DB) if REDIS_HOST else "-"
        return {
            "mode": self.redis.mode,
            "status_label": self.redis.status_label,
            "host": host,
            "port": port,
            "db": db,
        }

    def ensure_mongo_ready(self) -> None:
        now = time.time()
        if self.mongo.mode == "real":
            if now - self._last_mongo_probe >= self._mongo_probe_interval:
                try:
                    self.mongo.client.admin.command("ping")
                    self._last_mongo_probe = now
                except Exception:
                    self.mongo = MongoLite(MONGO_DIR)
                    self._last_mongo_probe = now
            self.mongo.ensure_structures()
            return

        if now - self._last_mongo_probe < self._mongo_probe_interval:
            self.mongo.ensure_structures()
            return

        preferred = self._create_mongo_store()
        self.mongo = preferred
        self._last_mongo_probe = now
        self.mongo.ensure_structures()

    def mongo_runtime(self) -> dict[str, str]:
        self.ensure_mongo_ready()
        return {
            "mode": self.mongo.mode,
            "status_label": self.mongo.status_label,
            "uri": MONGO_URI or "内置回退文档存储",
            "db": MONGO_DB_NAME,
        }

    def graph_runtime(self) -> dict[str, str]:
        return self.graph.runtime()

    def ensure_ready(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        MONGO_DIR.mkdir(parents=True, exist_ok=True)
        if not self.db_path.exists():
            self._create_schema()
            self._seed_data()
            self.ensure_redis_ready()
        else:
            self.ensure_mongo_ready()
            self._bootstrap_mongo_features()
            self.ensure_redis_ready()
        self._ensure_demo_enhancements()

    def reset_demo(self) -> None:
        self.ensure_mongo_ready()
        self.ensure_redis_ready()
        self._rank_bootstrap_checked = False
        if self.db_path.exists():
            self.db_path.unlink()
        self.redis.reset()
        self.mongo.reset()
        self._create_schema()
        self._seed_data()
        self.ensure_redis_ready()
        self._ensure_demo_enhancements()

    def _ensure_demo_enhancements(self) -> None:
        with self.connect() as conn:
            course = conn.execute(
                "SELECT course_id FROM courses WHERE course_code = ?",
                ("BD315",),
            ).fetchone()
            if not course:
                cursor = conn.execute(
                    "INSERT INTO courses (course_code, course_name, credit, course_type) VALUES (?, ?, ?, ?)",
                    ("BD315", "数据挖掘专题", 2.5, "专业选修"),
                )
                course_id = int(cursor.lastrowid)
            else:
                course_id = int(course["course_id"])

            offering = conn.execute(
                """
                SELECT offering_id, capacity, selected_count
                FROM course_offerings
                WHERE course_id = ? AND term = ?
                """,
                (course_id, CURRENT_TERM),
            ).fetchone()
            if not offering:
                cursor = conn.execute(
                    """
                    INSERT INTO course_offerings (course_id, teacher_id, term, capacity, selected_count, classroom, schedule_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (course_id, 3, CURRENT_TERM, 45, 0, "致远楼 B301", "周二 1-2 节"),
                )
                offering_id = int(cursor.lastrowid)
                remaining = 45
            else:
                offering_id = int(offering["offering_id"])
                remaining = int(offering["capacity"]) - int(offering["selected_count"])

        self.ensure_redis_ready()
        self.redis.set(f"course:quota:{offering_id}", remaining)
        self.redis.zadd("rank:course:current", str(offering_id), 0)

    def _campus_geo_spaces(self) -> list[dict[str, Any]]:
        return [
            {
                "spaceCode": "LIB-3F",
                "spaceName": "图书馆三楼自习室",
                "spaceType": "library",
                "building": "图书馆",
                "location": {"type": "Point", "coordinates": [116.39710, 39.90872]},
            },
            {
                "spaceCode": "LIB-4F",
                "spaceName": "图书馆四楼研讨区",
                "spaceType": "library",
                "building": "图书馆",
                "location": {"type": "Point", "coordinates": [116.39735, 39.90895]},
            },
            {
                "spaceCode": "LAB-A",
                "spaceName": "数据实验室 A",
                "spaceType": "lab",
                "building": "实验楼 1",
                "location": {"type": "Point", "coordinates": [116.39830, 39.90918]},
            },
            {
                "spaceCode": "LAB-COLLAB",
                "spaceName": "协同创新室",
                "spaceType": "lab",
                "building": "实验楼 2",
                "location": {"type": "Point", "coordinates": [116.39885, 39.90942]},
            },
            {
                "spaceCode": "LAB-SANDBOX",
                "spaceName": "企业沙盘室",
                "spaceType": "lab",
                "building": "经管楼",
                "location": {"type": "Point", "coordinates": [116.39672, 39.90815]},
            },
            {
                "spaceCode": "CLS-A305",
                "spaceName": "博学楼 A305",
                "spaceType": "classroom",
                "building": "博学楼",
                "location": {"type": "Point", "coordinates": [116.39792, 39.90968]},
            },
        ]

    def _mongo_asset_paths(self) -> list[Path]:
        return sorted((self.base_dir / "screenshots").glob("*.png"))

    def _bootstrap_mongo_features(self) -> None:
        self.mongo.upsert_geo_spaces(self._campus_geo_spaces())
        self.mongo.gridfs_sync_assets(self._mongo_asset_paths())
        if not self.mongo.count("realtime_event_feed"):
            self.mongo.insert_event_feed("system.bootstrap", "已初始化 Mongo 创新集合：Capped、TTL、Geo、GridFS。")
        cache_stats = self.mongo.search_cache_stats()
        if cache_stats["count"] == 0:
            self.mongo.cache_search("Redis", source="seed")
            self.mongo.cache_search("MongoDB", source="seed")

    def _graph_dataset(self) -> dict[str, Any]:
        return {
            "students": self._fetch_all("SELECT * FROM students ORDER BY student_id"),
            "teachers": self._fetch_all("SELECT * FROM teachers ORDER BY teacher_id"),
            "courses": self._fetch_all("SELECT * FROM courses ORDER BY course_id"),
            "offerings": self._fetch_all(
                """
                SELECT co.offering_id, co.course_id, co.teacher_id, co.term, co.classroom,
                       t.teacher_no, c.course_code
                FROM course_offerings co
                JOIN teachers t ON t.teacher_id = co.teacher_id
                JOIN courses c ON c.course_id = co.course_id
                ORDER BY co.offering_id
                """
            ),
            "selections": self._fetch_all(
                """
                SELECT cs.selection_id, cs.student_id, cs.selected_at, cs.status,
                       s.student_no, c.course_id, c.course_code
                FROM course_selections cs
                JOIN students s ON s.student_id = cs.student_id
                JOIN course_offerings co ON co.offering_id = cs.offering_id
                JOIN courses c ON c.course_id = co.course_id
                ORDER BY cs.selection_id
                """
            ),
            "scores": self._fetch_all(
                """
                SELECT sr.score_id, sr.student_id, sr.total_score, c.course_id
                FROM score_records sr
                JOIN course_offerings co ON co.offering_id = sr.offering_id
                JOIN courses c ON c.course_id = co.course_id
                ORDER BY sr.score_id
                """
            ),
            "books": self._fetch_all("SELECT * FROM books ORDER BY book_id"),
            "borrowings": self._fetch_all(
                """
                SELECT br.record_id, br.student_id, br.book_id, br.status, s.student_no, b.isbn
                FROM borrow_records br
                JOIN students s ON s.student_id = br.student_id
                JOIN books b ON b.book_id = br.book_id
                ORDER BY br.record_id
                """
            ),
            "projects": self._fetch_all("SELECT * FROM practice_projects ORDER BY project_id"),
            "lab_rooms": self._fetch_all("SELECT * FROM lab_rooms ORDER BY room_id"),
            "tasks": self._fetch_all(
                """
                SELECT it.task_id, it.student_id, it.base_name, it.mentor_name, it.project_title, it.progress, s.student_no
                FROM internship_tasks it
                JOIN students s ON s.student_id = it.student_id
                ORDER BY it.task_id
                """
            ),
            "lab_bookings": self._fetch_all(
                """
                SELECT lb.booking_id, lb.student_id, lb.room_id, lb.project_id, lb.booking_date,
                       s.student_no, pp.project_name
                FROM lab_bookings lb
                JOIN students s ON s.student_id = lb.student_id
                JOIN practice_projects pp ON pp.project_id = lb.project_id
                ORDER BY lb.booking_id
                """
            ),
        }

    def graph_overview(self, student_id: int | None = None) -> dict[str, Any]:
        students = self.list_students()
        graph_dataset = self._graph_dataset()
        default_student_id = self.graph.default_student_id(graph_dataset, [student["student_id"] for student in students])
        selected_student_id = student_id or default_student_id or students[0]["student_id"]
        selected_student = next((student for student in students if student["student_id"] == selected_student_id), students[0])
        return {
            "students": students,
            "selected_student_id": selected_student_id,
            "selected_student_name": selected_student["student_name"],
            "selected_student_major": selected_student["major"],
            "selected_student_node_id": f"student:{selected_student_id}",
            **self.graph.overview(graph_dataset, selected_student_id),
        }

    def governance_overview(self) -> dict[str, Any]:
        return self._build_governance_overview()

    @contextmanager
    def connect(self) -> Any:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _annotate_rank_items(self, items: list[dict[str, Any]], score_key: str = "score") -> list[dict[str, Any]]:
        if not items:
            return []
        top_score = max(int(item[score_key]) for item in items) or 1
        for item in items:
            score = int(item[score_key])
            item["bar_width"] = max(18, round(score / top_score * 100))
        return items

    def _latest_library_activity_time(self) -> datetime:
        row = self._fetch_one("SELECT MAX(borrowed_at) AS latest FROM borrow_records")
        return parse_time_text(row["latest"] if row else None)

    def _book_rank_window_defs(self, reference_time: datetime | None = None) -> list[dict[str, Any]]:
        reference_time = reference_time or self._latest_library_activity_time()
        iso = reference_time.isocalendar()
        day_key = f"rank:book:{reference_time.strftime('%Y%m%d')}"
        week_key = f"rank:book:{iso.year}wk{iso.week:02d}"
        month_key = f"rank:book:{reference_time.strftime('%Y%m')}"
        return [
            {
                "title": "日借阅榜",
                "key": day_key,
                "ttl_label": "24 小时自动淘汰",
                "subtitle": reference_time.strftime("统计窗口 %Y-%m-%d"),
                "trigger": "每次借阅实时更新",
            },
            {
                "title": "周借阅榜",
                "key": week_key,
                "ttl_label": "7 天后自动淘汰",
                "subtitle": f"统计窗口 {iso.year}wk{iso.week:02d}",
                "trigger": "每日汇总 + 借阅增量刷新",
            },
            {
                "title": "月借阅榜",
                "key": month_key,
                "ttl_label": "30 天后自动淘汰",
                "subtitle": reference_time.strftime("统计窗口 %Y-%m"),
                "trigger": "每日汇总 + 借阅增量刷新",
            },
            {
                "title": "总借阅榜",
                "key": "rank:book:total",
                "ttl_label": "长期保留",
                "subtitle": "累计借阅热度",
                "trigger": "每次借阅实时更新",
            },
        ]

    def _book_rank_keys(self, event_time: datetime, category: str | None = None) -> list[tuple[str, int | None]]:
        iso = event_time.isocalendar()
        keys: list[tuple[str, int | None]] = [
            (f"rank:book:{event_time.strftime('%Y%m%d')}", BOOK_RANK_TTLS["day"]),
            (f"rank:book:{iso.year}wk{iso.week:02d}", BOOK_RANK_TTLS["week"]),
            (f"rank:book:{event_time.strftime('%Y%m')}", BOOK_RANK_TTLS["month"]),
            ("rank:book:total", None),
        ]
        if category:
            keys.append((f"rank:book:category:{category}:{event_time.strftime('%Y%m%d')}", BOOK_RANK_TTLS["category_day"]))
        return keys

    def _update_book_rankings(
        self,
        book_id: int,
        category: str | None,
        amount: int = 1,
        event_time: datetime | None = None,
    ) -> None:
        event_time = event_time or datetime.now()
        for key, ttl in self._book_rank_keys(event_time, category):
            self.redis.zadd(key, str(book_id), amount, ex=ttl)

    def _load_book_rank_items(self, key: str, limit: int = 5) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for book_id, score in self.redis.ztop(key, limit=limit):
            book = self._fetch_one(
                "SELECT title, author, category, available_copies FROM books WHERE book_id = ?",
                (int(book_id),),
            )
            if book:
                items.append(
                    {
                        "title": book["title"],
                        "author": book["author"],
                        "category": book["category"],
                        "available_copies": book["available_copies"],
                        "score": score,
                    }
                )
        return self._annotate_rank_items(items)

    def _book_rankboards(self, reference_time: datetime | None = None, limit: int = 5) -> list[dict[str, Any]]:
        boards: list[dict[str, Any]] = []
        for board in self._book_rank_window_defs(reference_time):
            entries = self._load_book_rank_items(board["key"], limit=limit)
            boards.append({**board, "entries": entries})
        return boards

    def _category_rankboards(self, reference_time: datetime | None = None, limit: int = 3) -> list[dict[str, Any]]:
        reference_time = reference_time or self._latest_library_activity_time()
        day_code = reference_time.strftime("%Y%m%d")
        categories = self._fetch_all("SELECT DISTINCT category FROM books ORDER BY category")
        boards: list[dict[str, Any]] = []
        for row in categories:
            category = row["category"]
            key = f"rank:book:category:{category}:{day_code}"
            entries = self._load_book_rank_items(key, limit=limit)
            if entries:
                boards.append(
                    {
                        "title": f"{category}分类日榜",
                        "key": key,
                        "ttl_label": "24 小时自动淘汰",
                        "subtitle": reference_time.strftime("分类观察日 %Y-%m-%d"),
                        "entries": entries,
                        "top_score": entries[0]["score"],
                    }
                )
        boards.sort(key=lambda item: item["top_score"], reverse=True)
        return boards[:3]

    def _library_activity_stats(self, reference_time: datetime | None = None) -> list[dict[str, Any]]:
        reference_time = reference_time or self._latest_library_activity_time()
        day_start = reference_time.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        week_start = day_start - timedelta(days=reference_time.weekday())
        week_end = week_start + timedelta(days=7)
        month_start = day_start.replace(day=1)
        next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)

        def count_between(start: datetime, end: datetime) -> int:
            row = self._fetch_one(
                "SELECT COUNT(*) AS total FROM borrow_records WHERE borrowed_at >= ? AND borrowed_at < ?",
                (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
            )
            return int(row["total"]) if row else 0

        category_row = self._fetch_one(
            """
            SELECT b.category, COUNT(*) AS total
            FROM borrow_records br
            JOIN books b ON b.book_id = br.book_id
            WHERE br.borrowed_at >= ? AND br.borrowed_at < ?
            GROUP BY b.category
            ORDER BY total DESC, b.category
            LIMIT 1
            """,
            (month_start.strftime("%Y-%m-%d %H:%M:%S"), next_month.strftime("%Y-%m-%d %H:%M:%S")),
        )
        last_search = self.redis.get("library:last_search") or "暂无检索记录"
        return [
            {"label": "观察日", "value": day_start.strftime("%Y-%m-%d"), "detail": "用于展示日榜窗口"},
            {"label": "日借阅量", "value": count_between(day_start, day_end), "detail": "最新借阅日内的借阅次数"},
            {"label": "周借阅量", "value": count_between(week_start, week_end), "detail": "对应周榜窗口累计值"},
            {"label": "月热门类别", "value": category_row["category"] if category_row else "暂无", "detail": f"本月借阅最多，共 {category_row['total']} 次" if category_row else "等待新数据写入"},
            {"label": "最近检索词", "value": last_search, "detail": "Redis 中保留最近一次检索"},
        ]

    def _library_borrow_heatmap(self, reference_time: datetime | None = None, weeks: int = 3) -> dict[str, Any]:
        reference_time = reference_time or self._latest_library_activity_time()
        end_day = reference_time.replace(hour=0, minute=0, second=0, microsecond=0)
        start_day = end_day - timedelta(days=weeks * 7 - 1)
        weekday_labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        rows = self._fetch_all(
            """
            SELECT substr(borrowed_at, 1, 10) AS borrow_date, COUNT(*) AS total
            FROM borrow_records
            WHERE borrowed_at >= ? AND borrowed_at < ?
            GROUP BY substr(borrowed_at, 1, 10)
            ORDER BY borrow_date
            """,
            (start_day.strftime("%Y-%m-%d %H:%M:%S"), (end_day + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")),
        )
        count_map = {row["borrow_date"]: int(row["total"]) for row in rows}
        max_count = max(count_map.values(), default=0)
        peak_date = None
        peak_count = 0
        total_count = 0
        heatmap_rows: list[list[dict[str, Any]]] = []

        def level_for(count: int) -> int:
            if count <= 0:
                return 0
            if max_count <= 1:
                return 4
            ratio = count / max_count
            if ratio >= 0.8:
                return 4
            if ratio >= 0.55:
                return 3
            if ratio >= 0.3:
                return 2
            return 1

        for week_index in range(weeks):
            cells: list[dict[str, Any]] = []
            for day_index in range(7):
                current_day = start_day + timedelta(days=week_index * 7 + day_index)
                day_key = current_day.strftime("%Y-%m-%d")
                count = count_map.get(day_key, 0)
                total_count += count
                if count > peak_count:
                    peak_count = count
                    peak_date = day_key
                cells.append(
                    {
                        "date": day_key,
                        "date_short": current_day.strftime("%m-%d"),
                        "weekday": weekday_labels[day_index],
                        "count": count,
                        "level": level_for(count),
                        "level_class": f"level-{level_for(count)}",
                    }
                )
            heatmap_rows.append(cells)

        recent_total = sum(
            count_map.get((end_day - timedelta(days=offset)).strftime("%Y-%m-%d"), 0)
            for offset in range(7)
        )
        previous_total = sum(
            count_map.get((end_day - timedelta(days=offset)).strftime("%Y-%m-%d"), 0)
            for offset in range(7, 14)
        )
        delta = recent_total - previous_total
        if delta > 0:
            trend_label = f"近 7 天较前一周期增加 {delta} 次借阅"
        elif delta < 0:
            trend_label = f"近 7 天较前一周期减少 {abs(delta)} 次借阅"
        else:
            trend_label = "近 7 天与前一周期持平"

        return {
            "weekday_labels": weekday_labels,
            "rows": heatmap_rows,
            "total_count": total_count,
            "peak_date": peak_date or "暂无峰值",
            "peak_count": peak_count,
            "average_count": round(total_count / (weeks * 7), 1) if weeks else 0,
            "trend_label": trend_label,
            "range_label": f"{start_day.strftime('%m-%d')} 至 {end_day.strftime('%m-%d')}",
        }

    def _course_rankings(self, limit: int = 4) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for offering_id, score in self.redis.ztop("rank:course:current", limit=limit):
            row = self._fetch_one(
                """
                SELECT co.offering_id, co.capacity, co.selected_count, c.course_name, t.teacher_name
                FROM course_offerings co
                JOIN courses c ON c.course_id = co.course_id
                JOIN teachers t ON t.teacher_id = co.teacher_id
                WHERE co.offering_id = ?
                """,
                (int(offering_id),),
            )
            if row:
                selected_count = int(row["selected_count"])
                capacity = int(row["capacity"])
                remaining_count = max(capacity - selected_count, 0)
                fill_rate = round((selected_count / capacity) * 100) if capacity else 0
                items.append(
                    {
                        "name": row["course_name"],
                        "subline": f"{row['teacher_name']} · 满载率 {fill_rate}%",
                        "teacher_name": row["teacher_name"],
                        "meta": f"{selected_count}/{capacity} 人",
                        "score": score,
                        "selected_count": selected_count,
                        "capacity": capacity,
                        "remaining_count": remaining_count,
                        "fill_rate": fill_rate,
                        "chart_value": selected_count,
                    }
                )
        return self._annotate_rank_items(items)

    @staticmethod
    def _schedule_signature(schedule_text: str | None) -> str:
        return " ".join((schedule_text or "").split())

    def _academic_option_audit(
        self,
        student_id: int,
        offerings: list[dict[str, Any]],
        selections: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        selected_ids = {int(row["offering_id"]) for row in selections}
        selected_schedules: dict[str, list[str]] = {}
        for row in selections:
            signature = self._schedule_signature(row.get("schedule_text"))
            if not signature:
                continue
            selected_schedules.setdefault(signature, []).append(row["course_name"])

        audited: list[dict[str, Any]] = []
        for offering in offerings:
            remaining = int(offering["capacity"]) - int(offering["selected_count"])
            signature = self._schedule_signature(offering.get("schedule_text"))
            if int(offering["offering_id"]) in selected_ids:
                status = "已选"
                detail = "该课程已在当前课表中。"
                tone = "good"
                selectable = False
            elif signature and signature in selected_schedules:
                status = "时间冲突"
                detail = f"与《{selected_schedules[signature][0]}》安排在同一时段。"
                tone = "warn"
                selectable = False
            elif remaining <= 0:
                status = "名额不足"
                detail = "当前无剩余名额，可作为候补观察对象。"
                tone = "warn"
                selectable = False
            else:
                status = "可选"
                detail = f"当前无冲突，剩余 {remaining} 个名额。"
                tone = "info"
                selectable = True

            audited.append(
                {
                    "offering_id": offering["offering_id"],
                    "course_name": offering["course_name"],
                    "teacher_name": offering["teacher_name"],
                    "course_type": offering["course_type"],
                    "schedule_text": offering["schedule_text"],
                    "classroom": offering["classroom"],
                    "remaining": remaining,
                    "status": status,
                    "detail": detail,
                    "tone": tone,
                    "selectable": selectable,
                }
            )
        return audited

    def _academic_recommendations(
        self,
        student_id: int,
        offerings: list[dict[str, Any]],
        audited_options: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        student = self._fetch_one(
            "SELECT college, major, grade FROM students WHERE student_id = ?",
            (student_id,),
        ) or {"college": "", "major": "", "grade": ""}
        offering_lookup = {int(item["offering_id"]): item for item in offerings}
        recommendation_items: list[dict[str, Any]] = []

        for option in audited_options:
            if not option["selectable"]:
                continue

            offering_id = int(option["offering_id"])
            offering = offering_lookup[offering_id]
            peer_row = self._fetch_one(
                """
                SELECT COUNT(DISTINCT peer.student_id) AS total
                FROM course_selections base
                JOIN course_selections peer
                  ON base.offering_id = peer.offering_id
                 AND base.student_id <> peer.student_id
                 AND peer.status = 'selected'
                JOIN course_selections candidate
                  ON candidate.student_id = peer.student_id
                 AND candidate.offering_id = ?
                 AND candidate.status = 'selected'
                WHERE base.student_id = ? AND base.status = 'selected'
                """,
                (offering_id, student_id),
            )
            same_college_row = self._fetch_one(
                """
                SELECT COUNT(*) AS total
                FROM course_selections cs
                JOIN students s ON s.student_id = cs.student_id
                WHERE cs.offering_id = ? AND cs.status = 'selected' AND s.college = ?
                """,
                (offering_id, student["college"]),
            )
            same_grade_row = self._fetch_one(
                """
                SELECT COUNT(*) AS total
                FROM course_selections cs
                JOIN students s ON s.student_id = cs.student_id
                WHERE cs.offering_id = ? AND cs.status = 'selected' AND s.grade = ?
                """,
                (offering_id, student["grade"]),
            )
            peer_support = int(peer_row["total"]) if peer_row else 0
            college_support = int(same_college_row["total"]) if same_college_row else 0
            grade_support = int(same_grade_row["total"]) if same_grade_row else 0
            remaining = int(option["remaining"])
            popularity = int(offering["selected_count"])
            major_bonus = 10 if "专业" in offering["course_type"] else 4
            score = peer_support * 20 + college_support * 12 + grade_support * 8 + popularity * 6 + min(remaining, 12) + major_bonus
            reasons = ["当前课表无冲突"]
            if peer_support:
                reasons.append(f"有 {peer_support} 名修相近课程的同学也选了这门课")
            if college_support:
                reasons.append(f"同学院已有 {college_support} 人选修")
            reasons.append(f"当前剩余 {remaining} 个名额")
            recommendation_items.append(
                {
                    "name": offering["course_name"],
                    "subline": f"{offering['teacher_name']} · {offering['schedule_text']}",
                    "meta": f"{popularity} 人已选 · {remaining} 个名额",
                    "score": score,
                    "support": f"相似同学 {peer_support} 人 · 同学院 {college_support} 人 · 同年级 {grade_support} 人",
                    "reasons": reasons,
                }
            )

        ranked = sorted(recommendation_items, key=lambda item: (-item["score"], item["name"]))[:4]
        return self._annotate_rank_items(ranked)

    def _practice_progress_rankings(self, limit: int = 4) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for task_id, score in self.redis.ztop("rank:practice:progress", limit=limit):
            row = self._fetch_one(
                """
                SELECT it.task_id, it.project_title, it.mentor_name, it.weekly_count, s.student_name
                FROM internship_tasks it
                JOIN students s ON s.student_id = it.student_id
                WHERE it.task_id = ?
                """,
                (int(task_id),),
            )
            if row:
                items.append(
                    {
                        "name": row["student_name"],
                        "subline": f"{row['project_title']} · {row['mentor_name']}",
                        "meta": f"周报 {row['weekly_count']} 份",
                        "score": score,
                    }
                )
        return self._annotate_rank_items(items)

    def _lab_usage_rankings(self, limit: int = 4) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for room_id, score in self.redis.ztop("rank:practice:lab:usage", limit=limit):
            row = self._fetch_one("SELECT room_name, building, capacity FROM lab_rooms WHERE room_id = ?", (int(room_id),))
            if row:
                booking_count = int(score)
                capacity = int(row["capacity"])
                utilization_rate = round((booking_count / capacity) * 100) if capacity else 0
                items.append(
                    {
                        "name": row["room_name"],
                        "subline": f"{row['building']} · 容量 {row['capacity']} 人",
                        "building": row["building"],
                        "meta": f"累计预约 {booking_count} 次",
                        "score": score,
                        "capacity": capacity,
                        "booking_count": booking_count,
                        "utilization_rate": utilization_rate,
                        "chart_value": booking_count,
                    }
                )
        return self._annotate_rank_items(items)

    def _practice_risk_profiles(self, selected_student_id: int) -> dict[str, Any]:
        tasks = self._fetch_all(
            """
            SELECT it.task_id, it.student_id, it.base_name, it.mentor_name, it.project_title, it.progress, it.task_status, it.weekly_count,
                   s.student_name
            FROM internship_tasks it
            JOIN students s ON s.student_id = it.student_id
            ORDER BY it.progress ASC, it.weekly_count ASC
            """
        )
        profiles: list[dict[str, Any]] = []
        now = datetime.now()
        level_counter = {"高风险": 0, "中风险": 0, "低风险": 0}

        for task in tasks:
            attendance_row = self._fetch_one(
                """
                SELECT COUNT(*) AS total, MAX(sign_time) AS latest
                FROM attendance_records
                WHERE task_id = ?
                """,
                (task["task_id"],),
            ) or {"total": 0, "latest": None}
            report_row = self._fetch_one(
                """
                SELECT COUNT(*) AS total, MAX(created_at) AS latest
                FROM weekly_reports
                WHERE task_id = ?
                """,
                (task["task_id"],),
            ) or {"total": 0, "latest": None}
            booking_row = self._fetch_one(
                """
                SELECT COUNT(*) AS total, MAX(created_at) AS latest
                FROM lab_bookings
                WHERE student_id = ?
                """,
                (task["student_id"],),
            ) or {"total": 0, "latest": None}

            attendance_count = int(attendance_row["total"] or 0)
            report_count = int(report_row["total"] or 0)
            booking_count = int(booking_row["total"] or 0)
            progress = int(task["progress"])
            weekly_count = int(task["weekly_count"])
            score = 0
            rule_hits: list[str] = []
            suggestions: list[str] = []

            if progress < 50:
                score += 34
                rule_hits.append("任务进度低于 50%")
                suggestions.append("尽快拆分阶段目标并补录本周进展")
            elif progress < 65:
                score += 16
                rule_hits.append("任务进度低于理想值")
                suggestions.append("建议提高本周里程碑检查频率")

            if weekly_count < 2:
                score += 22
                rule_hits.append("周报数量偏少")
                suggestions.append("补齐周报与阶段总结，避免过程材料缺口")

            if attendance_count == 0:
                score += 24
                rule_hits.append("暂无签到记录")
                suggestions.append("补做签到或由导师确认现场参与情况")
            elif attendance_row.get("latest") and (now - parse_time_text(attendance_row["latest"])).days > 10:
                score += 10
                rule_hits.append("最近签到时间偏早")
                suggestions.append("补充近期待岗或现场记录")

            if report_count == 0:
                score += 28
                rule_hits.append("暂无周报正文")
                suggestions.append("尽快提交至少一份周报，形成过程留痕")
            elif report_row.get("latest") and (now - parse_time_text(report_row["latest"])).days > 10:
                score += 12
                rule_hits.append("最近周报更新滞后")
                suggestions.append("建议按周固定提交，避免阶段性断档")

            if booking_count == 0:
                score += 8
                rule_hits.append("缺少资源预约记录")
                suggestions.append("如涉及实验资源，建议补充预约或资源使用说明")

            if score >= 70:
                level = "高风险"
                tone = "warn"
            elif score >= 40:
                level = "中风险"
                tone = "mid"
            else:
                level = "低风险"
                tone = "good"

            if not rule_hits:
                rule_hits = ["当前过程节奏稳定"]
            if not suggestions:
                suggestions = ["继续保持周报、签到与阶段进度同步更新"]

            level_counter[level] += 1
            profile = {
                "task_id": task["task_id"],
                "student_id": task["student_id"],
                "student_name": task["student_name"],
                "project_title": task["project_title"],
                "base_name": task["base_name"],
                "mentor_name": task["mentor_name"],
                "progress": progress,
                "weekly_count": weekly_count,
                "attendance_count": attendance_count,
                "booking_count": booking_count,
                "risk_score": min(score, 99),
                "health_score": max(1, 100 - min(score, 99)),
                "risk_level": level,
                "tone": tone,
                "rule_hits": rule_hits,
                "suggestions": suggestions[:2],
                "last_report_at": report_row.get("latest") or "暂无周报",
                "last_sign_at": attendance_row.get("latest") or "暂无签到",
            }
            profiles.append(profile)

        profiles.sort(key=lambda item: (-item["risk_score"], item["student_name"]))
        focus = next((item for item in profiles if int(item["student_id"]) == int(selected_student_id)), profiles[0] if profiles else None)
        self.mongo.write_all(
            "practice_risk_profile",
            [
                {
                    "studentName": item["student_name"],
                    "projectTitle": item["project_title"],
                    "riskLevel": item["risk_level"],
                    "riskScore": item["risk_score"],
                    "ruleHitList": item["rule_hits"],
                    "suggestionList": item["suggestions"],
                    "progress": item["progress"],
                    "weeklyCount": item["weekly_count"],
                    "attendanceCount": item["attendance_count"],
                    "createdAt": now_text(),
                }
                for item in profiles
            ],
        )
        return {
            "profiles": profiles,
            "focus": focus,
            "summary": {
                "high": level_counter["高风险"],
                "mid": level_counter["中风险"],
                "low": level_counter["低风险"],
            },
        }

    def _leaderboard_catalog(self, reference_time: datetime | None = None) -> list[dict[str, Any]]:
        reference_time = reference_time or self._latest_library_activity_time()
        iso = reference_time.isocalendar()
        category_row = self._fetch_one("SELECT category FROM books ORDER BY category LIMIT 1")
        sample_category = category_row["category"] if category_row else "技术"
        return [
            {
                "scope": "图书日榜",
                "key": f"rank:book:{reference_time.strftime('%Y%m%d')}",
                "ttl": "24 小时后自动淘汰",
                "trigger": "每次借阅实时更新",
                "note": "适合课堂展示当天热门图书。",
            },
            {
                "scope": "图书周榜",
                "key": f"rank:book:{iso.year}wk{iso.week:02d}",
                "ttl": "7 天后自动淘汰",
                "trigger": "日汇总 + 借阅增量刷新",
                "note": "便于汇报一周阅读热点。",
            },
            {
                "scope": "图书月榜",
                "key": f"rank:book:{reference_time.strftime('%Y%m')}",
                "ttl": "30 天后自动淘汰",
                "trigger": "日汇总 + 借阅增量刷新",
                "note": "用于月度阅读分析。",
            },
            {
                "scope": "图书总榜",
                "key": "rank:book:total",
                "ttl": "长期保留",
                "trigger": "每次借阅实时更新",
                "note": "用于查看累计借阅热度。",
            },
            {
                "scope": "图书分类日榜",
                "key": f"rank:book:category:{sample_category}:{reference_time.strftime('%Y%m%d')}",
                "ttl": "24 小时后自动淘汰",
                "trigger": "同分类借阅实时更新",
                "note": "便于比较技术类、管理类等细分阅读倾向。",
            },
            {
                "scope": "课程热选榜",
                "key": "rank:course:current",
                "ttl": "长期保留",
                "trigger": "选课/退课实时调整",
                "note": "展示教务侧的供需热度。",
            },
            {
                "scope": "实验室利用榜",
                "key": "rank:practice:lab:usage",
                "ttl": "长期保留",
                "trigger": "预约成功实时更新",
                "note": "突出实践平台的资源利用分析。",
            },
            {
                "scope": "任务推进榜",
                "key": "rank:practice:progress",
                "ttl": "长期保留",
                "trigger": "周报提交后实时更新",
                "note": "适合讲过程管理和项目追踪。",
            },
        ]

    def _bootstrap_rank_indexes(self) -> None:
        with self.connect() as conn:
            borrow_rows = conn.execute(
                """
                SELECT br.book_id, br.borrowed_at, b.category
                FROM borrow_records br
                JOIN books b ON b.book_id = br.book_id
                ORDER BY br.borrowed_at
                """
            ).fetchall()
            if not self.redis.ztop("book:hot:rank", limit=1):
                for row in borrow_rows:
                    self.redis.zadd("book:hot:rank", str(row["book_id"]), 1)
            if not self.redis.ztop("rank:book:total", limit=1):
                for row in borrow_rows:
                    self._update_book_rankings(int(row["book_id"]), row["category"], event_time=parse_time_text(row["borrowed_at"]))

            if not self.redis.ztop("rank:course:current", limit=1):
                course_rows = conn.execute("SELECT offering_id, selected_count FROM course_offerings").fetchall()
                for row in course_rows:
                    self.redis.zadd("rank:course:current", str(row["offering_id"]), int(row["selected_count"]))

            if not self.redis.ztop("rank:practice:progress", limit=1):
                task_rows = conn.execute("SELECT task_id, progress FROM internship_tasks").fetchall()
                for row in task_rows:
                    self.redis.zadd("rank:practice:progress", str(row["task_id"]), int(row["progress"]))

            if not self.redis.ztop("rank:practice:lab:usage", limit=1):
                lab_rows = conn.execute(
                    "SELECT room_id, COUNT(*) AS booking_count FROM lab_bookings GROUP BY room_id"
                ).fetchall()
                for row in lab_rows:
                    self.redis.zadd("rank:practice:lab:usage", str(row["room_id"]), int(row["booking_count"]))

        if self.redis.get("teacher:dashboard:practice") is None:
            self.redis.set("teacher:dashboard:practice", {"ongoingProjects": 3, "onTimeTasks": 2, "riskTasks": 1})
        if self.redis.get("notice:due:2026-04-30") is None:
            self.redis.set("notice:due:2026-04-30", ["20230001", "20230003"])
        self._rank_bootstrap_checked = True

    def _create_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE students (
                    student_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_no TEXT NOT NULL UNIQUE,
                    student_name TEXT NOT NULL,
                    college TEXT NOT NULL,
                    major TEXT NOT NULL,
                    grade TEXT NOT NULL
                );
                CREATE TABLE teachers (
                    teacher_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    teacher_no TEXT NOT NULL UNIQUE,
                    teacher_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    department TEXT NOT NULL
                );
                CREATE TABLE books (
                    book_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    isbn TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL,
                    category TEXT NOT NULL,
                    shelf TEXT NOT NULL,
                    total_copies INTEGER NOT NULL,
                    available_copies INTEGER NOT NULL
                );
                CREATE TABLE borrow_records (
                    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    student_id INTEGER NOT NULL,
                    borrowed_at TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    returned_at TEXT,
                    status TEXT NOT NULL,
                    FOREIGN KEY(book_id) REFERENCES books(book_id),
                    FOREIGN KEY(student_id) REFERENCES students(student_id)
                );
                CREATE TABLE library_seats (
                    seat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_name TEXT NOT NULL,
                    seat_no TEXT NOT NULL,
                    seat_status TEXT NOT NULL
                );
                CREATE TABLE seat_reservations (
                    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seat_id INTEGER NOT NULL,
                    student_id INTEGER NOT NULL,
                    reserve_date TEXT NOT NULL,
                    time_slot TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(seat_id) REFERENCES library_seats(seat_id),
                    FOREIGN KEY(student_id) REFERENCES students(student_id)
                );
                CREATE TABLE courses (
                    course_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_code TEXT NOT NULL UNIQUE,
                    course_name TEXT NOT NULL,
                    credit REAL NOT NULL,
                    course_type TEXT NOT NULL
                );
                CREATE TABLE course_offerings (
                    offering_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_id INTEGER NOT NULL,
                    teacher_id INTEGER NOT NULL,
                    term TEXT NOT NULL,
                    capacity INTEGER NOT NULL,
                    selected_count INTEGER NOT NULL DEFAULT 0,
                    classroom TEXT NOT NULL,
                    schedule_text TEXT NOT NULL,
                    FOREIGN KEY(course_id) REFERENCES courses(course_id),
                    FOREIGN KEY(teacher_id) REFERENCES teachers(teacher_id)
                );
                CREATE TABLE course_selections (
                    selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offering_id INTEGER NOT NULL,
                    student_id INTEGER NOT NULL,
                    selected_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    FOREIGN KEY(offering_id) REFERENCES course_offerings(offering_id),
                    FOREIGN KEY(student_id) REFERENCES students(student_id)
                );
                CREATE TABLE score_records (
                    score_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offering_id INTEGER NOT NULL,
                    student_id INTEGER NOT NULL,
                    usual_score REAL NOT NULL,
                    final_score REAL NOT NULL,
                    total_score REAL NOT NULL,
                    FOREIGN KEY(offering_id) REFERENCES course_offerings(offering_id),
                    FOREIGN KEY(student_id) REFERENCES students(student_id)
                );
                CREATE TABLE practice_projects (
                    project_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_name TEXT NOT NULL,
                    course_name TEXT NOT NULL,
                    instructor TEXT NOT NULL,
                    location TEXT NOT NULL,
                    project_status TEXT NOT NULL,
                    progress INTEGER NOT NULL
                );
                CREATE TABLE lab_rooms (
                    room_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_name TEXT NOT NULL,
                    building TEXT NOT NULL,
                    capacity INTEGER NOT NULL,
                    room_status TEXT NOT NULL
                );
                CREATE TABLE lab_bookings (
                    booking_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id INTEGER NOT NULL,
                    project_id INTEGER NOT NULL,
                    student_id INTEGER NOT NULL,
                    booking_date TEXT NOT NULL,
                    time_slot TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(room_id) REFERENCES lab_rooms(room_id),
                    FOREIGN KEY(project_id) REFERENCES practice_projects(project_id),
                    FOREIGN KEY(student_id) REFERENCES students(student_id)
                );
                CREATE TABLE internship_tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL,
                    base_name TEXT NOT NULL,
                    mentor_name TEXT NOT NULL,
                    project_title TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    task_status TEXT NOT NULL,
                    weekly_count INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(student_id) REFERENCES students(student_id)
                );
                CREATE TABLE attendance_records (
                    attendance_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    sign_time TEXT NOT NULL,
                    location TEXT NOT NULL,
                    sign_status TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES internship_tasks(task_id)
                );
                CREATE TABLE weekly_reports (
                    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    week_no INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES internship_tasks(task_id)
                );
                CREATE INDEX idx_borrow_status_time ON borrow_records(status, borrowed_at DESC);
                CREATE INDEX idx_borrow_student_status ON borrow_records(student_id, status);
                CREATE UNIQUE INDEX idx_seat_slot_unique ON seat_reservations(seat_id, reserve_date, time_slot, status);
                CREATE INDEX idx_course_term_load ON course_offerings(term, selected_count DESC);
                CREATE INDEX idx_course_selection_student ON course_selections(student_id, status);
                CREATE UNIQUE INDEX idx_course_selection_unique ON course_selections(offering_id, student_id, status);
                CREATE INDEX idx_score_student ON score_records(student_id);
                CREATE INDEX idx_lab_booking_slot ON lab_bookings(room_id, booking_date, time_slot, status);
                CREATE INDEX idx_task_student ON internship_tasks(student_id, task_status);
                CREATE INDEX idx_attendance_task_time ON attendance_records(task_id, sign_time DESC);
                CREATE UNIQUE INDEX idx_weekly_report_unique ON weekly_reports(task_id, week_no);
                CREATE INDEX idx_weekly_report_time ON weekly_reports(created_at DESC);
                """
            )

    def _seed_data(self) -> None:
        self.ensure_mongo_ready()
        with self.connect() as conn:
            conn.executemany(
                "INSERT INTO students (student_no, student_name, college, major, grade) VALUES (?, ?, ?, ?, ?)",
                [
                    ("20230001", "张三", "计算机学院", "数据科学与大数据技术", "2023级"),
                    ("20230002", "李四", "计算机学院", "软件工程", "2023级"),
                    ("20230003", "王敏", "信息学院", "信息管理与信息系统", "2023级"),
                    ("20230004", "陈晨", "人工智能学院", "计算机科学与技术", "2022级"),
                ],
            )
            conn.executemany(
                "INSERT INTO teachers (teacher_no, teacher_name, title, department) VALUES (?, ?, ?, ?)",
                [
                    ("T1001", "刘海燕", "副教授", "图书馆信息中心"),
                    ("T1002", "周立群", "教授", "教务处"),
                    ("T1003", "赵新宇", "讲师", "实践教学中心"),
                ],
            )
            conn.executemany(
                "INSERT INTO books (isbn, title, author, category, shelf, total_copies, available_copies) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("9787302512345", "数据库系统概论", "王珊", "教材", "A1-03", 6, 4),
                    ("9787111543210", "Redis 设计与实现", "黄健宏", "技术", "A2-06", 4, 3),
                    ("9787302657893", "MongoDB 权威指南", "Kristina Chodorow", "技术", "A2-07", 3, 3),
                    ("9787121450003", "数据治理方法论", "李志刚", "管理", "B1-08", 5, 5),
                    ("9787302580009", "Python 数据分析实战", "张良均", "技术", "A3-01", 5, 4),
                ],
            )
            conn.executemany(
                "INSERT INTO borrow_records (book_id, student_id, borrowed_at, due_at, returned_at, status) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (1, 1, "2026-04-01 09:10:00", "2026-05-01 09:10:00", None, "borrowing"),
                    (2, 2, "2026-03-20 15:20:00", "2026-04-20 15:20:00", "2026-03-29 18:20:00", "returned"),
                    (5, 3, "2026-04-02 10:00:00", "2026-05-02 10:00:00", None, "borrowing"),
                ],
            )
            conn.executemany(
                "INSERT INTO library_seats (room_name, seat_no, seat_status) VALUES (?, ?, ?)",
                [
                    ("图书馆三楼自习室", "A-101", "idle"),
                    ("图书馆三楼自习室", "A-102", "idle"),
                    ("图书馆三楼自习室", "A-103", "idle"),
                    ("图书馆四楼研讨区", "B-201", "idle"),
                    ("图书馆四楼研讨区", "B-202", "idle"),
                ],
            )
            conn.executemany(
                "INSERT INTO seat_reservations (seat_id, student_id, reserve_date, time_slot, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(1, 1, "2026-04-07", "08:00-10:00", "reserved", now_text())],
            )
            conn.executemany(
                "INSERT INTO courses (course_code, course_name, credit, course_type) VALUES (?, ?, ?, ?)",
                [
                    ("BD301", "大数据管理", 3.0, "专业核心"),
                    ("SE214", "软件需求分析", 2.5, "专业必修"),
                    ("AI220", "机器学习导论", 3.0, "专业选修"),
                    ("IM305", "信息资源组织", 2.0, "专业方向"),
                    ("BD315", "数据挖掘专题", 2.5, "专业选修"),
                ],
            )
            conn.executemany(
                "INSERT INTO course_offerings (course_id, teacher_id, term, capacity, selected_count, classroom, schedule_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, 2, CURRENT_TERM, 80, 2, "博学楼 A305", "周二 1-2 节"),
                    (2, 2, CURRENT_TERM, 60, 1, "博学楼 B102", "周三 3-4 节"),
                    (3, 3, CURRENT_TERM, 50, 1, "致远楼 C201", "周四 5-6 节"),
                    (4, 1, CURRENT_TERM, 40, 1, "图书馆报告厅", "周五 1-2 节"),
                    (5, 3, CURRENT_TERM, 45, 0, "致远楼 B301", "周二 1-2 节"),
                ],
            )
            conn.executemany(
                "INSERT INTO course_selections (offering_id, student_id, selected_at, status) VALUES (?, ?, ?, ?)",
                [
                    (1, 1, "2026-03-01 10:00:00", "selected"),
                    (1, 2, "2026-03-01 10:08:00", "selected"),
                    (2, 3, "2026-03-02 14:00:00", "selected"),
                    (3, 1, "2026-03-03 09:30:00", "selected"),
                    (4, 4, "2026-03-04 11:15:00", "selected"),
                ],
            )
            conn.executemany(
                "INSERT INTO score_records (offering_id, student_id, usual_score, final_score, total_score) VALUES (?, ?, ?, ?, ?)",
                [
                    (1, 1, 86, 90, 88),
                    (1, 2, 78, 82, 80),
                    (2, 3, 75, 69, 72),
                    (3, 1, 92, 94, 93),
                    (4, 4, 60, 58, 59),
                ],
            )
            conn.executemany(
                "INSERT INTO practice_projects (project_name, course_name, instructor, location, project_status, progress) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("校园能耗监测看板", "实践教学项目一", "赵新宇", "实验中心 101", "进行中", 72),
                    ("智慧图书馆座位分析", "实践教学项目二", "刘海燕", "图书馆创新工坊", "进行中", 64),
                    ("企业数据治理调研", "认识实习", "周立群", "校外基地 A", "已立项", 35),
                ],
            )
            conn.executemany(
                "INSERT INTO lab_rooms (room_name, building, capacity, room_status) VALUES (?, ?, ?, ?)",
                [
                    ("数据实验室 A", "实验楼 1", 48, "可预约"),
                    ("协同创新室", "实验楼 2", 30, "可预约"),
                    ("企业沙盘室", "经管楼", 36, "维护中"),
                ],
            )
            conn.executemany(
                "INSERT INTO lab_bookings (room_id, project_id, student_id, booking_date, time_slot, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, 1, 1, "2026-04-08", "14:00-16:00", "approved", now_text()),
                    (2, 2, 3, "2026-04-09", "10:00-12:00", "approved", now_text()),
                ],
            )
            conn.executemany(
                "INSERT INTO internship_tasks (student_id, base_name, mentor_name, project_title, progress, task_status, weekly_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, "校图书馆数字资源部", "刘海燕", "阅读行为画像分析", 70, "进行中", 3),
                    (2, "市大数据中心", "周立群", "高校数据目录梳理", 55, "进行中", 2),
                    (3, "校园信息化办公室", "赵新宇", "实验课预约优化", 40, "进行中", 1),
                ],
            )
            conn.executemany(
                "INSERT INTO attendance_records (task_id, sign_time, location, sign_status) VALUES (?, ?, ?, ?)",
                [
                    (1, "2026-04-05 08:35:00", "图书馆创新工坊", "success"),
                    (2, "2026-04-05 09:00:00", "市大数据中心", "success"),
                ],
            )
            conn.executemany(
                "INSERT INTO weekly_reports (task_id, week_no, content, created_at) VALUES (?, ?, ?, ?)",
                [
                    (1, 3, "完成图书借阅日志清洗，并输出借阅热力分析初稿。", now_text()),
                    (2, 2, "完成数据目录字段梳理，补充共享接口清单。", now_text()),
                ],
            )
        self._seed_redis()
        self._seed_mongo()
        self._bootstrap_mongo_features()

    def _seed_redis(self) -> None:
        with self.connect() as conn:
            offerings = conn.execute("SELECT offering_id, capacity, selected_count FROM course_offerings").fetchall()
            for row in offerings:
                remaining = row["capacity"] - row["selected_count"]
                self.redis.set(f"course:quota:{row['offering_id']}", remaining)
                self.redis.zadd("rank:course:current", str(row["offering_id"]), int(row["selected_count"]))

            ranking = conn.execute(
                """
                SELECT br.book_id, br.borrowed_at, b.category
                FROM borrow_records br
                JOIN books b ON b.book_id = br.book_id
                ORDER BY br.borrowed_at
                """
            ).fetchall()
            for row in ranking:
                self.redis.zadd("book:hot:rank", str(row["book_id"]), 1)
                self._update_book_rankings(int(row["book_id"]), row["category"], event_time=parse_time_text(row["borrowed_at"]))

            task_rows = conn.execute("SELECT task_id, progress FROM internship_tasks").fetchall()
            for row in task_rows:
                self.redis.zadd("rank:practice:progress", str(row["task_id"]), int(row["progress"]))

            lab_rows = conn.execute(
                "SELECT room_id, COUNT(*) AS booking_count FROM lab_bookings GROUP BY room_id"
            ).fetchall()
            for row in lab_rows:
                self.redis.zadd("rank:practice:lab:usage", str(row["room_id"]), int(row["booking_count"]))

        self.redis.set("teacher:dashboard:practice", {"ongoingProjects": 3, "onTimeTasks": 2, "riskTasks": 1})
        self.redis.set("notice:due:2026-04-30", ["20230001", "20230003"])
        self._rank_bootstrap_checked = True

    def _seed_mongo(self) -> None:
        self.mongo.insert(
            "library_behavior_log",
            {"studentName": "张三", "action": "search_book", "keyword": "Redis", "device": "PC"},
        )
        self.mongo.insert(
            "library_behavior_log",
            {"studentName": "王敏", "action": "borrow_book", "bookTitle": "Python 数据分析实战", "device": "Mobile"},
        )
        self.mongo.insert(
            "teaching_change_log",
            {"operator": "教务秘书", "changeType": "容量调整", "target": "大数据管理", "beforeValue": 70, "afterValue": 80},
        )
        self.mongo.insert(
            "warning_profile",
            {"studentName": "陈晨", "riskType": "成绩预警", "ruleHitList": ["单科不及格", "总评低于 60"]},
        )
        self.mongo.insert(
            "internship_weekly_report",
            {"studentName": "张三", "weekNo": 3, "content": "完成阅读行为画像分析可视化。"},
        )
        self.mongo.insert(
            "evaluation_comment",
            {"studentName": "李四", "comment": "调研材料扎实，建议补充数据标准化章节。"},
        )

    def _fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def _fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(query, params).fetchone()
        return dict(row) if row else None

    def list_students(self) -> list[dict[str, Any]]:
        return self._fetch_all("SELECT * FROM students ORDER BY student_id")

    @staticmethod
    def _ratio_score(passed: int | float, total: int | float) -> float:
        return round((passed / total) * 100, 1) if total else 100.0

    @staticmethod
    def _score_label(score: float) -> str:
        if score >= 99:
            return "优秀"
        if score >= 95:
            return "良好"
        if score >= 85:
            return "关注"
        return "整改"

    @staticmethod
    def _filled(value: Any) -> bool:
        return value not in (None, "", [])

    def _build_governance_overview(self) -> dict[str, Any]:
        self.ensure_mongo_ready()
        self._bootstrap_mongo_features()

        student_count = self._fetch_one("SELECT COUNT(*) AS total FROM students")["total"]
        teacher_count = self._fetch_one("SELECT COUNT(*) AS total FROM teachers")["total"]
        course_count = self._fetch_one("SELECT COUNT(*) AS total FROM courses")["total"]
        book_count = self._fetch_one("SELECT COUNT(*) AS total FROM books")["total"]
        lab_room_count = self._fetch_one("SELECT COUNT(*) AS total FROM lab_rooms")["total"]
        seat_count = self._fetch_one("SELECT COUNT(*) AS total FROM library_seats")["total"]
        geo_space_count = self.mongo.count("campus_space_geo")

        master_catalog = [
            {
                "entity": "学生主数据",
                "business_key": "student_no",
                "owner": "教务处",
                "count": student_count,
                "scope": "图书馆 / 教务 / 实践三端统一身份",
                "storage": "SQLite students + 图节点 Student",
                "downstream": "borrow_records / course_selections / internship_tasks / graph",
            },
            {
                "entity": "教师主数据",
                "business_key": "teacher_no",
                "owner": "教务处",
                "count": teacher_count,
                "scope": "课程开课、师资画像、图谱授课关系",
                "storage": "SQLite teachers + 图节点 Teacher",
                "downstream": "course_offerings / graph",
            },
            {
                "entity": "课程主数据",
                "business_key": "course_code",
                "owner": "二级学院",
                "count": course_count,
                "scope": "选课、成绩、课程推荐共用",
                "storage": "SQLite courses + 图节点 Course",
                "downstream": "course_offerings / score_records / graph",
            },
            {
                "entity": "图书主数据",
                "business_key": "isbn",
                "owner": "图书馆",
                "count": book_count,
                "scope": "借阅、排行榜、推荐与分类分析",
                "storage": "SQLite books + 图节点 Book",
                "downstream": "borrow_records / Redis ranking / graph",
            },
            {
                "entity": "空间主数据",
                "business_key": "spaceCode",
                "owner": "实验中心",
                "count": lab_room_count + seat_count + geo_space_count,
                "scope": "图书馆座位、实验室、Geo 空间统一管理",
                "storage": "SQLite seats & lab_rooms + Mongo campus_space_geo",
                "downstream": "seat_reservations / lab_bookings / geo query",
            },
        ]

        completeness_specs = [
            ("students", ["student_no", "student_name", "college", "major", "grade"]),
            ("teachers", ["teacher_no", "teacher_name", "title", "department"]),
            ("books", ["isbn", "title", "author", "category", "shelf"]),
            ("courses", ["course_code", "course_name", "course_type"]),
            ("lab_rooms", ["room_name", "building", "room_status"]),
        ]
        completeness_passed = 0
        completeness_total = 0
        for table, columns in completeness_specs:
            rows = self._fetch_all(f"SELECT {', '.join(columns)} FROM {table}")
            completeness_total += len(rows) * len(columns)
            completeness_passed += sum(1 for row in rows for column in columns if self._filled(row[column]))
        completeness_score = self._ratio_score(completeness_passed, completeness_total)

        uniqueness_specs = [
            ("students", "student_no", "学号"),
            ("teachers", "teacher_no", "工号"),
            ("books", "isbn", "ISBN"),
            ("courses", "course_code", "课程编码"),
        ]
        uniqueness_passed = 0
        uniqueness_total = 0
        uniqueness_rules: list[dict[str, Any]] = []
        for table, column, label in uniqueness_specs:
            total = self._fetch_one(f"SELECT COUNT(*) AS total FROM {table}")["total"]
            distinct_total = self._fetch_one(f"SELECT COUNT(DISTINCT {column}) AS total FROM {table}")["total"]
            uniqueness_total += total
            uniqueness_passed += distinct_total
            score = self._ratio_score(distinct_total, total)
            uniqueness_rules.append(
                {
                    "rule": f"{label}唯一性",
                    "passed": distinct_total,
                    "total": total,
                    "score": score,
                    "status": "通过" if distinct_total == total else "关注",
                    "detail": f"{table}.{column} 作为主数据业务键",
                }
            )
        uniqueness_score = self._ratio_score(uniqueness_passed, uniqueness_total)

        book_rule_total = self._fetch_one("SELECT COUNT(*) AS total FROM books")["total"]
        book_rule_passed = self._fetch_one(
            "SELECT COUNT(*) AS total FROM books WHERE available_copies >= 0 AND available_copies <= total_copies"
        )["total"]
        offering_rule_total = self._fetch_one("SELECT COUNT(*) AS total FROM course_offerings")["total"]
        offering_rule_passed = self._fetch_one(
            "SELECT COUNT(*) AS total FROM course_offerings WHERE selected_count >= 0 AND selected_count <= capacity"
        )["total"]
        project_rule_total = self._fetch_one("SELECT COUNT(*) AS total FROM practice_projects")["total"]
        project_rule_passed = self._fetch_one(
            "SELECT COUNT(*) AS total FROM practice_projects WHERE progress >= 0 AND progress <= 100"
        )["total"]
        task_rule_total = self._fetch_one("SELECT COUNT(*) AS total FROM internship_tasks")["total"]
        task_rule_passed = self._fetch_one(
            "SELECT COUNT(*) AS total FROM internship_tasks WHERE progress >= 0 AND progress <= 100 AND weekly_count >= 0"
        )["total"]
        consistency_passed = book_rule_passed + offering_rule_passed + project_rule_passed + task_rule_passed
        consistency_total = book_rule_total + offering_rule_total + project_rule_total + task_rule_total
        consistency_score = self._ratio_score(consistency_passed, consistency_total)

        integrity_specs = [
            (
                "借阅外键完整性",
                "SELECT COUNT(*) AS total FROM borrow_records",
                """
                SELECT COUNT(*) AS total
                FROM borrow_records br
                JOIN students s ON s.student_id = br.student_id
                JOIN books b ON b.book_id = br.book_id
                """,
                "borrow_records.student_id/book_id 可追溯到学生与图书主数据",
            ),
            (
                "选课外键完整性",
                "SELECT COUNT(*) AS total FROM course_selections",
                """
                SELECT COUNT(*) AS total
                FROM course_selections cs
                JOIN students s ON s.student_id = cs.student_id
                JOIN course_offerings co ON co.offering_id = cs.offering_id
                """,
                "course_selections 关联学生主数据与开课事实",
            ),
            (
                "预约外键完整性",
                "SELECT COUNT(*) AS total FROM lab_bookings",
                """
                SELECT COUNT(*) AS total
                FROM lab_bookings lb
                JOIN students s ON s.student_id = lb.student_id
                JOIN lab_rooms lr ON lr.room_id = lb.room_id
                JOIN practice_projects pp ON pp.project_id = lb.project_id
                """,
                "实验室预约同时关联学生、空间、项目三类主数据",
            ),
            (
                "签到外键完整性",
                "SELECT COUNT(*) AS total FROM attendance_records",
                """
                SELECT COUNT(*) AS total
                FROM attendance_records ar
                JOIN internship_tasks it ON it.task_id = ar.task_id
                """,
                "attendance_records 可以回溯到实践任务主数据",
            ),
        ]
        integrity_passed = 0
        integrity_total = 0
        integrity_rules: list[dict[str, Any]] = []
        for name, total_query, passed_query, detail in integrity_specs:
            total = self._fetch_one(total_query)["total"]
            passed = self._fetch_one(passed_query)["total"]
            integrity_total += total
            integrity_passed += passed
            score = self._ratio_score(passed, total)
            integrity_rules.append(
                {
                    "rule": name,
                    "passed": passed,
                    "total": total,
                    "score": score,
                    "status": "通过" if passed == total else "关注",
                    "detail": detail,
                }
            )
        integrity_score = self._ratio_score(integrity_passed, integrity_total)

        time_candidates: list[datetime] = []
        for query in [
            "SELECT MAX(borrowed_at) AS latest FROM borrow_records",
            "SELECT MAX(created_at) AS latest FROM seat_reservations",
            "SELECT MAX(selected_at) AS latest FROM course_selections",
            "SELECT MAX(created_at) AS latest FROM lab_bookings",
            "SELECT MAX(created_at) AS latest FROM weekly_reports",
        ]:
            row = self._fetch_one(query)
            if row and row["latest"]:
                time_candidates.append(parse_time_text(row["latest"]))
        mongo_events = self.mongo.recent_event_feed(limit=1)
        if mongo_events:
            time_candidates.append(parse_time_text(mongo_events[0].get("createdAt")))
        latest_update = max(time_candidates) if time_candidates else datetime.now()
        days_gap = max((datetime.now() - latest_update).days, 0)
        if days_gap <= 7:
            timeliness_score = 100.0
            timeliness_status = "优秀"
        elif days_gap <= 30:
            timeliness_score = 92.0
            timeliness_status = "良好"
        elif days_gap <= 60:
            timeliness_score = 80.0
            timeliness_status = "关注"
        else:
            timeliness_score = 65.0
            timeliness_status = "整改"

        quality_dimensions = [
            {
                "name": "完整性",
                "score": completeness_score,
                "status": self._score_label(completeness_score),
                "detail": f"核心主数据字段填充 {completeness_passed}/{completeness_total}",
            },
            {
                "name": "唯一性",
                "score": uniqueness_score,
                "status": self._score_label(uniqueness_score),
                "detail": f"学号 / 工号 / ISBN / 课程编码唯一通过 {uniqueness_passed}/{uniqueness_total}",
            },
            {
                "name": "一致性",
                "score": consistency_score,
                "status": self._score_label(consistency_score),
                "detail": f"库存、容量、进度等约束通过 {consistency_passed}/{consistency_total}",
            },
            {
                "name": "完整关联性",
                "score": integrity_score,
                "status": self._score_label(integrity_score),
                "detail": f"跨表外键与业务关联通过 {integrity_passed}/{integrity_total}",
            },
            {
                "name": "时效性",
                "score": timeliness_score,
                "status": timeliness_status,
                "detail": f"最新业务更新时间 {latest_update.strftime('%Y-%m-%d %H:%M:%S')}",
            },
        ]
        quality_score = round(sum(item["score"] for item in quality_dimensions) / len(quality_dimensions), 1)

        quality_rules = [
            *uniqueness_rules,
            {
                "rule": "图书库存约束",
                "passed": book_rule_passed,
                "total": book_rule_total,
                "score": self._ratio_score(book_rule_passed, book_rule_total),
                "status": "通过" if book_rule_passed == book_rule_total else "关注",
                "detail": "available_copies 必须位于 0 与 total_copies 之间",
            },
            {
                "rule": "开课容量约束",
                "passed": offering_rule_passed,
                "total": offering_rule_total,
                "score": self._ratio_score(offering_rule_passed, offering_rule_total),
                "status": "通过" if offering_rule_passed == offering_rule_total else "关注",
                "detail": "selected_count 不得超过 capacity",
            },
            {
                "rule": "实践进度约束",
                "passed": project_rule_passed + task_rule_passed,
                "total": project_rule_total + task_rule_total,
                "score": self._ratio_score(project_rule_passed + task_rule_passed, project_rule_total + task_rule_total),
                "status": "通过" if (project_rule_passed == project_rule_total and task_rule_passed == task_rule_total) else "关注",
                "detail": "项目与任务 progress 必须落在 0-100 区间",
            },
            *integrity_rules,
            {
                "rule": "近 30 天数据新鲜度",
                "passed": 1 if days_gap <= 30 else 0,
                "total": 1,
                "score": timeliness_score,
                "status": "通过" if days_gap <= 30 else "关注",
                "detail": f"距离最新业务数据 {days_gap} 天",
            },
        ]

        lineage_flow = [
            {
                "stage": "业务主数据层",
                "detail": "students / teachers / books / courses / spaces 在 SQLite 中统一维护业务主键。",
                "value": "学号、工号、ISBN、课程编码、空间编码",
            },
            {
                "stage": "共享缓存层",
                "detail": "Redis 只保存排行、配额、摘要等高频访问结果，不复制主数据语义。",
                "value": "rank:book:* / rank:course:* / quota:*",
            },
            {
                "stage": "文档治理层",
                "detail": "MongoDB 保存行为日志、事件流、Geo 空间、GridFS 资产与质量快照。",
                "value": "Validator / TTL / Geo / GridFS / Snapshot",
            },
            {
                "stage": "关系分析层",
                "detail": "图谱把主数据映射为节点和关系，支持推荐、多跳查询和中心性分析。",
                "value": "Cypher Export / NetworkX / Neo4j Ready",
            },
        ]

        innovation_points = [
            "统一业务主键：学号、工号、ISBN、课程编码贯穿多子系统，符合主数据管理思路。",
            "MongoDB 新增数据质量快照集合，把治理结果文档化，便于展示治理留痕。",
            "Redis 只缓存衍生指标，不把缓存当主数据源，体现数据职责分层。",
            "图数据库扩展直接复用主数据业务键，能讲清主数据到图建模的映射关系。",
            "跨库写入按照“SQLite 主事务优先，Redis 与 MongoDB 随后同步”的方式组织，便于说明最终一致性策略。",
            "图谱模块通过 Cypher 导出与本地图分析解耦，后续接入 Neo4j 时只需调整连接配置即可切换。",
        ]

        recommendations = [
            "可引入冷热数据分层：Redis 保存热数据，MongoDB 保存近一年的温数据，历史日志再归档到 HDFS 或对象存储。",
            "可在抢课高峰前增加 Kafka 或 RabbitMQ，对选课请求进行削峰填谷，后台按固定速率消费并写入主库。",
            "可把数据质量快照和跨库补偿做成定时任务，定期从 SQLite 对账 Redis 与 MongoDB，强化最终一致性保障。",
        ]

        snapshot_at = now_text()
        snapshot_document = {
            "snapshotName": "campus_master_quality",
            "summary": f"主数据综合质量 {quality_score} 分，覆盖 {len(master_catalog)} 类核心实体。",
            "totalScore": quality_score,
            "snapshotDate": snapshot_at[:10],
            "dimensions": [{"name": item["name"], "score": item["score"]} for item in quality_dimensions],
            "masterEntities": [{"entity": item["entity"], "count": item["count"]} for item in master_catalog],
            "createdAt": snapshot_at,
        }
        self.mongo.write_all("data_quality_snapshot", [snapshot_document])

        return {
            "quality_score": quality_score,
            "quality_label": self._score_label(quality_score),
            "quality_score_text": f"{quality_score:.1f}",
            "snapshot_at": snapshot_at,
            "master_catalog": master_catalog,
            "quality_dimensions": quality_dimensions,
            "quality_rules": quality_rules,
            "lineage_flow": lineage_flow,
            "innovation_points": innovation_points,
            "recommendations": recommendations,
            "master_entity_count": len(master_catalog),
            "master_record_count": sum(item["count"] for item in master_catalog),
        }

    def dashboard_context(self) -> dict[str, Any]:
        self.ensure_redis_ready()
        self.ensure_mongo_ready()
        students = self.list_students()
        if students:
            self._practice_risk_profiles(students[0]["student_id"])
        reference_time = self._latest_library_activity_time()
        stats = {
            "student_count": self._fetch_one("SELECT COUNT(*) AS total FROM students")["total"],
            "teacher_count": self._fetch_one("SELECT COUNT(*) AS total FROM teachers")["total"],
            "active_borrows": self._fetch_one("SELECT COUNT(*) AS total FROM borrow_records WHERE status = 'borrowing'")["total"],
            "active_selections": self._fetch_one("SELECT COUNT(*) AS total FROM course_selections WHERE status = 'selected'")["total"],
            "ongoing_tasks": self._fetch_one("SELECT COUNT(*) AS total FROM internship_tasks WHERE task_status = '进行中'")["total"],
            "mongo_docs": sum(
                self.mongo.count(name)
                for name in [
                    "library_behavior_log",
                    "teaching_change_log",
                    "warning_profile",
                    "internship_weekly_report",
                    "evaluation_comment",
                    "practice_risk_profile",
                ]
            ),
        }
        rank_boards = self._book_rankboards(reference_time, limit=4)
        hot_books = rank_boards[-1]["entries"] if rank_boards else []
        project_board = self._fetch_all(
            "SELECT project_name, instructor, progress, project_status FROM practice_projects ORDER BY progress DESC"
        )
        architecture = [
            {"layer": "MySQL/SQLite 事务层", "detail": "学生、课程、借阅、预约、成绩等强一致业务数据"},
            {"layer": "Redis 缓存层", "detail": "多粒度排行榜、选课名额、座位锁、教师看板摘要"},
            {"layer": "Mongo 文档层", "detail": "行为日志、变更留痕、周报文本、预警画像"},
        ]
        compliance_matrix = [
            {"title": "整体设计方案", "status": "已完成", "detail": "首页总览、数据中心和多数据库分层架构已形成统一展示入口。"},
            {"title": "智慧图书馆子系统", "status": "已完成", "detail": "覆盖借阅、归还、检索、座位预约和多粒度热榜。"},
            {"title": "智慧教务子系统", "status": "已完成", "detail": "覆盖选课、容量状态、成绩画像、冲突预检和课程推荐。"},
            {"title": "实践教学子系统", "status": "已完成", "detail": "覆盖实验室预约、签到、周报、进度管理和风险画像。"},
            {"title": "编程实现", "status": "已超额", "detail": "不是只实现一个子系统，而是把三类业务模块统一落成可运行演示系统。"},
            {"title": "异构数据库要求", "status": "已满足", "detail": "SQLite 承担结构化事务，Redis 承担缓存与排行榜，MongoDB 承担日志和画像。"},
        ]
        subsystem_matrix = [
            {
                "name": "智慧图书馆",
                "structured": "SQLite: 图书、借阅、座位、预约",
                "non_structured": "Redis: 日榜/周榜/月榜/分类榜；Mongo: 检索与行为日志",
            },
            {
                "name": "智慧教务",
                "structured": "SQLite: 课程、开课、选课、成绩",
                "non_structured": "Redis: 热选榜与剩余名额；Mongo: 变更留痕与预警画像",
            },
            {
                "name": "实践教学",
                "structured": "SQLite: 项目、预约、签到、任务",
                "non_structured": "Redis: 进度榜与资源摘要；Mongo: 周报正文、评语、风险文档",
            },
        ]
        defense_highlights = [
            "三大业务子系统共用主数据口径，便于讲清数据治理和业务主键统一。",
            "Redis 不直接保存主业务事实，只保存排行榜、名额和摘要，职责边界清晰。",
            "MongoDB 保存日志、画像、周报和质量快照，适合回答为什么要用非关系数据库。",
            "图谱实验室把课程、图书和实践关系抽象成图模型，属于明显的加分扩展项。",
        ]
        return {
            "stats": stats,
            "hot_books": hot_books,
            "rank_boards": rank_boards,
            "rank_reference": reference_time.strftime("%Y-%m-%d %H:%M"),
            "course_rankings": self._course_rankings(limit=4),
            "lab_rankings": self._lab_usage_rankings(limit=4),
            "project_board": project_board,
            "architecture": architecture,
            "redis_keys": self.redis.summary(limit=10),
            "compliance_matrix": compliance_matrix,
            "subsystem_matrix": subsystem_matrix,
            "defense_highlights": defense_highlights,
        }

    def library_overview(self, keyword: str = "") -> dict[str, Any]:
        self.ensure_redis_ready()
        self.ensure_mongo_ready()
        keyword = keyword.strip()
        if keyword:
            books = self._fetch_all(
                """
                SELECT * FROM books
                WHERE title LIKE ? OR author LIKE ? OR category LIKE ?
                ORDER BY title
                """,
                (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"),
            )
            self.redis.set("library:last_search", keyword, ex=1800)
            self.mongo.insert("library_behavior_log", {"studentName": "系统访客", "action": "search_book", "keyword": keyword})
            self.mongo.cache_search(keyword, source="library-search")
            self.mongo.insert_event_feed("library.search", f"触发图书检索：{keyword}", {"keyword": keyword})
        else:
            books = self._fetch_all("SELECT * FROM books ORDER BY title")
        borrowings = self._fetch_all(
            """
            SELECT br.record_id, s.student_name, b.title, br.borrowed_at, br.due_at, br.status
            FROM borrow_records br
            JOIN students s ON s.student_id = br.student_id
            JOIN books b ON b.book_id = br.book_id
            WHERE br.status = 'borrowing'
            ORDER BY br.borrowed_at DESC
            """
        )
        reservations = self._fetch_all(
            """
            SELECT sr.reservation_id, s.student_name, ls.room_name, ls.seat_no, sr.reserve_date, sr.time_slot, sr.status
            FROM seat_reservations sr
            JOIN students s ON s.student_id = sr.student_id
            JOIN library_seats ls ON ls.seat_id = sr.seat_id
            ORDER BY sr.created_at DESC
            """
        )
        students = self.list_students()
        seats = self._fetch_all("SELECT * FROM library_seats WHERE seat_status = 'idle' ORDER BY room_name, seat_no")
        reference_time = self._latest_library_activity_time()
        rank_boards = self._book_rankboards(reference_time, limit=4)
        category_boards = self._category_rankboards(reference_time, limit=3)
        hot_books = rank_boards[-1]["entries"] if rank_boards else []
        return {
            "students": students,
            "books": books,
            "borrowings": borrowings,
            "reservations": reservations,
            "seats": seats,
            "hot_books": hot_books,
            "rank_boards": rank_boards,
            "category_boards": category_boards,
            "library_insights": self._library_activity_stats(reference_time),
            "borrow_heatmap": self._library_borrow_heatmap(reference_time),
            "leaderboard_catalog": self._leaderboard_catalog(reference_time)[:5],
            "rank_reference": reference_time.strftime("%Y-%m-%d %H:%M"),
            "logs": self.mongo.recent("library_behavior_log", limit=6),
            "keyword": keyword,
        }

    def borrow_book(self, student_id: int, book_id: int) -> tuple[bool, str]:
        self.ensure_redis_ready()
        self.ensure_mongo_ready()
        event_time = datetime.now()
        with self.connect() as conn:
            book = conn.execute("SELECT title, category, available_copies FROM books WHERE book_id = ?", (book_id,)).fetchone()
            student = conn.execute("SELECT student_name FROM students WHERE student_id = ?", (student_id,)).fetchone()
            if not book or not student:
                return False, "借阅失败：学生或图书不存在。"
            if book["available_copies"] <= 0:
                return False, f"《{book['title']}》当前无可借副本。"
            conn.execute("UPDATE books SET available_copies = available_copies - 1 WHERE book_id = ?", (book_id,))
            conn.execute(
                "INSERT INTO borrow_records (book_id, student_id, borrowed_at, due_at, returned_at, status) VALUES (?, ?, ?, ?, ?, ?)",
                (book_id, student_id, event_time.strftime("%Y-%m-%d %H:%M:%S"), (event_time + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"), None, "borrowing"),
            )
        self.redis.delete(f"book:detail:{book_id}")
        self.redis.incr(f"user:borrow:count:{student_id}")
        self.redis.zadd("book:hot:rank", str(book_id), 1)
        self._update_book_rankings(book_id, book["category"], event_time=event_time)
        self.mongo.insert(
            "library_behavior_log",
            {"studentName": student["student_name"], "action": "borrow_book", "bookTitle": book["title"], "device": "Web Demo"},
        )
        self.mongo.insert_event_feed("library.borrow", f"{student['student_name']} 借阅《{book['title']}》", {"bookId": book_id})
        return True, f"{student['student_name']} 已成功借阅《{book['title']}》。"

    def return_book(self, record_id: int) -> tuple[bool, str]:
        self.ensure_redis_ready()
        self.ensure_mongo_ready()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT br.record_id, br.student_id, br.book_id, s.student_name, b.title
                FROM borrow_records br
                JOIN students s ON s.student_id = br.student_id
                JOIN books b ON b.book_id = br.book_id
                WHERE br.record_id = ? AND br.status = 'borrowing'
                """,
                (record_id,),
            ).fetchone()
            if not row:
                return False, "归还失败：未找到有效借阅记录。"
            conn.execute("UPDATE borrow_records SET status = 'returned', returned_at = ? WHERE record_id = ?", (now_text(), record_id))
            conn.execute("UPDATE books SET available_copies = available_copies + 1 WHERE book_id = ?", (row["book_id"],))
            active_count = conn.execute(
                "SELECT COUNT(*) AS total FROM borrow_records WHERE student_id = ? AND status = 'borrowing'",
                (row["student_id"],),
            ).fetchone()["total"]
        self.redis.set(f"user:borrow:count:{row['student_id']}", active_count)
        self.mongo.insert(
            "library_behavior_log",
            {"studentName": row["student_name"], "action": "return_book", "bookTitle": row["title"], "device": "Web Demo"},
        )
        self.mongo.insert_event_feed("library.return", f"{row['student_name']} 归还《{row['title']}》", {"recordId": record_id})
        return True, f"{row['student_name']} 已归还《{row['title']}》。"

    def reserve_seat(self, student_id: int, seat_id: int, reserve_date: str, time_slot: str) -> tuple[bool, str]:
        self.ensure_redis_ready()
        self.ensure_mongo_ready()
        lock_key = f"seat:lock:{seat_id}:{reserve_date}:{time_slot}"
        if not self.redis.setnx(lock_key, "locked", ex=30):
            return False, "该座位正在被其他请求处理中，请稍后再试。"
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT 1 FROM seat_reservations
                WHERE seat_id = ? AND reserve_date = ? AND time_slot = ? AND status = 'reserved'
                """,
                (seat_id, reserve_date, time_slot),
            ).fetchone()
            student = conn.execute("SELECT student_name FROM students WHERE student_id = ?", (student_id,)).fetchone()
            seat = conn.execute("SELECT room_name, seat_no FROM library_seats WHERE seat_id = ?", (seat_id,)).fetchone()
            if existing:
                return False, "该座位该时段已被预约。"
            conn.execute(
                "INSERT INTO seat_reservations (seat_id, student_id, reserve_date, time_slot, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (seat_id, student_id, reserve_date, time_slot, "reserved", now_text()),
            )
        self.mongo.insert(
            "library_behavior_log",
            {"studentName": student["student_name"], "action": "reserve_seat", "seat": f"{seat['room_name']} {seat['seat_no']}", "reserveDate": reserve_date},
        )
        self.mongo.insert_event_feed(
            "library.reserve",
            f"{student['student_name']} 预约 {seat['room_name']} {seat['seat_no']}",
            {"seatId": seat_id, "timeSlot": time_slot},
        )
        return True, f"{student['student_name']} 已预约 {seat['room_name']} {seat['seat_no']}。"

    def academic_overview(self, student_id: int | None = None) -> dict[str, Any]:
        self.ensure_redis_ready()
        self.ensure_mongo_ready()
        students = self.list_students()
        selected_student_id = student_id or students[0]["student_id"]
        offerings = self._fetch_all(
            """
            SELECT co.offering_id, c.course_name, c.course_type, c.credit, t.teacher_name, co.capacity, co.selected_count, co.classroom, co.schedule_text
            FROM course_offerings co
            JOIN courses c ON c.course_id = co.course_id
            JOIN teachers t ON t.teacher_id = co.teacher_id
            WHERE co.term = ?
            ORDER BY c.course_name
            """,
            (CURRENT_TERM,),
        )
        for offering in offerings:
            remaining = offering["capacity"] - offering["selected_count"]
            self.redis.set(f"course:quota:{offering['offering_id']}", remaining)
        selections = self._fetch_all(
            """
            SELECT cs.selection_id, co.offering_id, c.course_name, c.course_type, t.teacher_name, co.classroom, co.schedule_text, cs.status
            FROM course_selections cs
            JOIN course_offerings co ON co.offering_id = cs.offering_id
            JOIN courses c ON c.course_id = co.course_id
            JOIN teachers t ON t.teacher_id = co.teacher_id
            WHERE cs.student_id = ? AND cs.status = 'selected'
            ORDER BY c.course_name
            """,
            (selected_student_id,),
        )
        scores = self._fetch_all(
            """
            SELECT c.course_name, sr.usual_score, sr.final_score, sr.total_score
            FROM score_records sr
            JOIN course_offerings co ON co.offering_id = sr.offering_id
            JOIN courses c ON c.course_id = co.course_id
            WHERE sr.student_id = ?
            ORDER BY c.course_name
            """,
            (selected_student_id,),
        )
        timetable = [{"course": row["course_name"], "schedule": row["schedule_text"], "classroom": row["classroom"]} for row in selections]
        self.redis.set(f"timetable:student:{selected_student_id}:{CURRENT_TERM}", timetable, ex=43200)
        warnings = self._fetch_all(
            """
            SELECT s.student_name, ROUND(AVG(sr.total_score), 1) AS avg_score,
                   SUM(CASE WHEN sr.total_score < 60 THEN 1 ELSE 0 END) AS fail_count
            FROM score_records sr
            JOIN students s ON s.student_id = sr.student_id
            GROUP BY s.student_id, s.student_name
            HAVING avg_score < 75 OR fail_count > 0
            ORDER BY avg_score ASC
            """
        )
        capacity_alerts = []
        for offering in sorted(
            offerings,
            key=lambda row: (row["selected_count"] / row["capacity"]) if row["capacity"] else 0,
            reverse=True,
        )[:3]:
            fill_rate = round((offering["selected_count"] / offering["capacity"]) * 100) if offering["capacity"] else 0
            capacity_alerts.append(
                {
                    "course_name": offering["course_name"],
                    "detail": f"{offering['teacher_name']} · {offering['classroom']}",
                    "value": f"{fill_rate}%",
                }
            )
        option_audit = self._academic_option_audit(selected_student_id, offerings, selections)
        return {
            "students": students,
            "selected_student_id": selected_student_id,
            "offerings": offerings,
            "selections": selections,
            "scores": scores,
            "warnings": warnings,
            "course_rankings": self._course_rankings(limit=4),
            "course_option_audit": option_audit,
            "course_recommendations": self._academic_recommendations(selected_student_id, offerings, option_audit),
            "capacity_alerts": capacity_alerts,
            "logs": self.mongo.recent("teaching_change_log", limit=6),
            "profiles": self.mongo.recent("warning_profile", limit=4),
            "term": CURRENT_TERM,
        }

    def select_course(self, student_id: int, offering_id: int) -> tuple[bool, str]:
        self.ensure_redis_ready()
        self.ensure_mongo_ready()
        with self.connect() as conn:
            offering = conn.execute(
                """
                SELECT co.offering_id, co.capacity, co.selected_count, c.course_name
                FROM course_offerings co
                JOIN courses c ON c.course_id = co.course_id
                WHERE co.offering_id = ?
                """,
                (offering_id,),
            ).fetchone()
            student = conn.execute("SELECT student_name FROM students WHERE student_id = ?", (student_id,)).fetchone()
            existing = conn.execute(
                "SELECT 1 FROM course_selections WHERE offering_id = ? AND student_id = ? AND status = 'selected'",
                (offering_id, student_id),
            ).fetchone()
            conflict = conn.execute(
                """
                SELECT c.course_name
                FROM course_selections cs
                JOIN course_offerings co ON co.offering_id = cs.offering_id
                JOIN courses c ON c.course_id = co.course_id
                JOIN course_offerings target ON target.offering_id = ?
                WHERE cs.student_id = ? AND cs.status = 'selected' AND co.schedule_text = target.schedule_text
                LIMIT 1
                """,
                (offering_id, student_id),
            ).fetchone()
            if not offering or not student:
                return False, "选课失败：数据不存在。"
            if existing:
                return False, f"{student['student_name']} 已选择《{offering['course_name']}》。"
            if conflict:
                return False, f"选课失败：与已选《{conflict['course_name']}》时间冲突。"
            quota_key = f"course:quota:{offering_id}"
            remaining = int(self.redis.get(quota_key) or (offering["capacity"] - offering["selected_count"]))
            if remaining <= 0:
                return False, f"《{offering['course_name']}》已无剩余名额。"
            conn.execute(
                "INSERT INTO course_selections (offering_id, student_id, selected_at, status) VALUES (?, ?, ?, ?)",
                (offering_id, student_id, now_text(), "selected"),
            )
            conn.execute("UPDATE course_offerings SET selected_count = selected_count + 1 WHERE offering_id = ?", (offering_id,))
        self.redis.set(quota_key, remaining - 1)
        self.redis.zadd("rank:course:current", str(offering_id), 1)
        self.mongo.insert(
            "teaching_change_log",
            {"operator": student["student_name"], "changeType": "学生选课", "target": offering["course_name"], "afterValue": remaining - 1},
        )
        self.mongo.insert_event_feed("academic.select", f"{student['student_name']} 选上《{offering['course_name']}》", {"offeringId": offering_id})
        return True, f"{student['student_name']} 已选上《{offering['course_name']}》。"

    def drop_course(self, selection_id: int) -> tuple[bool, str, int | None]:
        self.ensure_redis_ready()
        self.ensure_mongo_ready()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT cs.selection_id, cs.student_id, co.offering_id, s.student_name, c.course_name
                FROM course_selections cs
                JOIN course_offerings co ON co.offering_id = cs.offering_id
                JOIN courses c ON c.course_id = co.course_id
                JOIN students s ON s.student_id = cs.student_id
                WHERE cs.selection_id = ? AND cs.status = 'selected'
                """,
                (selection_id,),
            ).fetchone()
            if not row:
                return False, "退课失败：未找到有效选课记录。", None
            conn.execute("UPDATE course_selections SET status = 'dropped' WHERE selection_id = ?", (selection_id,))
            conn.execute("UPDATE course_offerings SET selected_count = selected_count - 1 WHERE offering_id = ?", (row["offering_id"],))
            remaining = conn.execute(
                "SELECT capacity - selected_count AS remaining FROM course_offerings WHERE offering_id = ?",
                (row["offering_id"],),
            ).fetchone()["remaining"]
        self.redis.set(f"course:quota:{row['offering_id']}", remaining)
        self.redis.zadd("rank:course:current", str(row["offering_id"]), -1)
        self.mongo.insert(
            "teaching_change_log",
            {"operator": row["student_name"], "changeType": "学生退课", "target": row["course_name"], "afterValue": remaining},
        )
        self.mongo.insert_event_feed("academic.drop", f"{row['student_name']} 退选《{row['course_name']}》", {"selectionId": selection_id})
        return True, f"{row['student_name']} 已退选《{row['course_name']}》。", row["student_id"]

    def practice_overview(self, student_id: int | None = None) -> dict[str, Any]:
        self.ensure_redis_ready()
        self.ensure_mongo_ready()
        students = self.list_students()
        selected_student_id = student_id or students[0]["student_id"]
        projects = self._fetch_all("SELECT * FROM practice_projects ORDER BY progress DESC")
        rooms = self._fetch_all("SELECT * FROM lab_rooms ORDER BY room_status DESC, room_name")
        tasks = self._fetch_all(
            """
            SELECT it.task_id, s.student_name, it.base_name, it.mentor_name, it.project_title, it.progress, it.task_status, it.weekly_count
            FROM internship_tasks it
            JOIN students s ON s.student_id = it.student_id
            ORDER BY it.progress DESC
            """
        )
        bookings = self._fetch_all(
            """
            SELECT lb.booking_id, s.student_name, lr.room_name, pp.project_name, lb.booking_date, lb.time_slot, lb.status
            FROM lab_bookings lb
            JOIN students s ON s.student_id = lb.student_id
            JOIN lab_rooms lr ON lr.room_id = lb.room_id
            JOIN practice_projects pp ON pp.project_id = lb.project_id
            ORDER BY lb.created_at DESC
            """
        )
        reports = self._fetch_all(
            """
            SELECT wr.report_id, s.student_name, wr.week_no, wr.content, wr.created_at
            FROM weekly_reports wr
            JOIN internship_tasks it ON it.task_id = wr.task_id
            JOIN students s ON s.student_id = it.student_id
            ORDER BY wr.created_at DESC
            """
        )
        attendance = self._fetch_all(
            """
            SELECT ar.attendance_id, s.student_name, ar.sign_time, ar.location, ar.sign_status
            FROM attendance_records ar
            JOIN internship_tasks it ON it.task_id = ar.task_id
            JOIN students s ON s.student_id = it.student_id
            ORDER BY ar.sign_time DESC
            """
        )
        for task in tasks:
            self.redis.set(
                f"project:progress:{task['task_id']}",
                {"student": task["student_name"], "progress": task["progress"], "status": task["task_status"]},
                ex=604800,
            )
        risk_analysis = self._practice_risk_profiles(selected_student_id)
        return {
            "students": students,
            "selected_student_id": selected_student_id,
            "projects": projects,
            "rooms": rooms,
            "tasks": tasks,
            "bookings": bookings,
            "reports": reports,
            "attendance": attendance,
            "task_rankings": self._practice_progress_rankings(limit=4),
            "lab_rankings": self._lab_usage_rankings(limit=4),
            "risk_profiles": risk_analysis["profiles"],
            "focus_risk_profile": risk_analysis["focus"],
            "risk_summary": risk_analysis["summary"],
            "mongo_reports": self.mongo.recent("internship_weekly_report", limit=5),
            "mongo_comments": self.mongo.recent("evaluation_comment", limit=4),
            "mongo_risks": self.mongo.recent("practice_risk_profile", limit=4),
            "teacher_dashboard": self.redis.get("teacher:dashboard:practice") or {},
        }

    def book_lab(self, student_id: int, room_id: int, project_id: int, booking_date: str, time_slot: str) -> tuple[bool, str]:
        self.ensure_redis_ready()
        self.ensure_mongo_ready()
        quota_key = f"lab:quota:{room_id}:{booking_date}:{time_slot}"
        if self.redis.get(quota_key) is None:
            self.redis.set(quota_key, 1, ex=86400)
        remaining = int(self.redis.get(quota_key) or 0)
        if remaining <= 0:
            return False, "该实验室时段已无剩余名额。"
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM lab_bookings WHERE room_id = ? AND booking_date = ? AND time_slot = ? AND status = 'approved'",
                (room_id, booking_date, time_slot),
            ).fetchone()
            room = conn.execute("SELECT room_name FROM lab_rooms WHERE room_id = ?", (room_id,)).fetchone()
            project = conn.execute("SELECT project_name FROM practice_projects WHERE project_id = ?", (project_id,)).fetchone()
            student = conn.execute("SELECT student_name FROM students WHERE student_id = ?", (student_id,)).fetchone()
            if existing:
                self.redis.set(quota_key, 0, ex=86400)
                return False, "该实验室时段已被预约。"
            conn.execute(
                "INSERT INTO lab_bookings (room_id, project_id, student_id, booking_date, time_slot, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (room_id, project_id, student_id, booking_date, time_slot, "approved", now_text()),
            )
        self.redis.set(quota_key, remaining - 1, ex=86400)
        self.redis.zadd("rank:practice:lab:usage", str(room_id), 1)
        self.mongo.insert(
            "evaluation_comment",
            {"studentName": student["student_name"], "comment": f"已预约 {room['room_name']} 用于项目《{project['project_name']}》演示准备。"},
        )
        self.mongo.insert_event_feed("practice.book_lab", f"{student['student_name']} 预约 {room['room_name']}", {"roomId": room_id})
        return True, f"{student['student_name']} 已预约 {room['room_name']}。"

    def sign_attendance(self, task_id: int, location: str) -> tuple[bool, str]:
        self.ensure_redis_ready()
        self.ensure_mongo_ready()
        token_key = f"sign:token:{task_id}"
        self.redis.set(token_key, "SIGNED", ex=900)
        with self.connect() as conn:
            task = conn.execute(
                """
                SELECT it.task_id, s.student_name, it.project_title
                FROM internship_tasks it
                JOIN students s ON s.student_id = it.student_id
                WHERE it.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if not task:
                return False, "签到失败：未找到实践任务。"
            conn.execute(
                "INSERT INTO attendance_records (task_id, sign_time, location, sign_status) VALUES (?, ?, ?, ?)",
                (task_id, now_text(), location, "success"),
            )
        self.mongo.insert(
            "internship_weekly_report",
            {"studentName": task["student_name"], "weekNo": "签到", "content": f"已在 {location} 完成《{task['project_title']}》签到。"},
        )
        self.mongo.insert_event_feed("practice.sign", f"{task['student_name']} 在 {location} 完成签到", {"taskId": task_id})
        return True, f"{task['student_name']} 已完成签到。"

    def submit_weekly_report(self, task_id: int, week_no: int, content: str) -> tuple[bool, str]:
        self.ensure_redis_ready()
        self.ensure_mongo_ready()
        content = content.strip()
        if not content:
            return False, "周报内容不能为空。"
        with self.connect() as conn:
            task = conn.execute(
                """
                SELECT it.task_id, it.student_id, it.progress, s.student_name
                FROM internship_tasks it
                JOIN students s ON s.student_id = it.student_id
                WHERE it.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if not task:
                return False, "提交失败：未找到实践任务。"
            conn.execute(
                "INSERT INTO weekly_reports (task_id, week_no, content, created_at) VALUES (?, ?, ?, ?)",
                (task_id, week_no, content, now_text()),
            )
            next_progress = min(100, int(task["progress"]) + 8)
            conn.execute(
                "UPDATE internship_tasks SET weekly_count = weekly_count + 1, progress = ? WHERE task_id = ?",
                (next_progress, task_id),
            )
        progress_delta = next_progress - int(task["progress"])
        self.redis.set(
            f"project:progress:{task_id}",
            {"student": task["student_name"], "progress": next_progress, "status": "进行中"},
            ex=604800,
        )
        if progress_delta:
            self.redis.zadd("rank:practice:progress", str(task_id), progress_delta)
        self.mongo.insert(
            "internship_weekly_report",
            {"studentName": task["student_name"], "weekNo": week_no, "content": content},
        )
        self.mongo.insert_event_feed("practice.report", f"{task['student_name']} 提交第 {week_no} 周周报", {"taskId": task_id})
        return True, f"{task['student_name']} 的第 {week_no} 周周报已提交。"

    def data_center_overview(self) -> dict[str, Any]:
        self.ensure_redis_ready()
        self.ensure_mongo_ready()
        self._bootstrap_mongo_features()
        governance = self._build_governance_overview()
        students = self.list_students()
        if students:
            self._practice_risk_profiles(students[0]["student_id"])
        reference_time = self._latest_library_activity_time()
        sql_summary = [
            {"name": "students", "count": self._fetch_one("SELECT COUNT(*) AS total FROM students")["total"]},
            {"name": "borrow_records", "count": self._fetch_one("SELECT COUNT(*) AS total FROM borrow_records")["total"]},
            {"name": "course_selections", "count": self._fetch_one("SELECT COUNT(*) AS total FROM course_selections")["total"]},
            {"name": "internship_tasks", "count": self._fetch_one("SELECT COUNT(*) AS total FROM internship_tasks")["total"]},
            {"name": "weekly_reports", "count": self._fetch_one("SELECT COUNT(*) AS total FROM weekly_reports")["total"]},
        ]
        mongo_collections = [
            {"name": name, "count": self.mongo.count(name), "recent": self.mongo.recent(name, limit=3)}
            for name in [
                "library_behavior_log",
                "teaching_change_log",
                "warning_profile",
                "internship_weekly_report",
                "evaluation_comment",
                "practice_risk_profile",
                "data_quality_snapshot",
            ]
        ]
        graph_dataset = self._graph_dataset()
        default_graph_student_id = self.graph.default_student_id(
            graph_dataset,
            [student["student_id"] for student in students],
        )
        graph_snapshot = self.graph.overview(graph_dataset, default_graph_student_id or 1)
        return {
            "sql_summary": sql_summary,
            "redis_keys": self.redis.summary(limit=20),
            "mongo_collections": mongo_collections,
            "mongo_feature_catalog": self.mongo.collection_catalog(),
            "mongo_aggregation": self.mongo.aggregation_showcase(),
            "mongo_event_feed": self.mongo.recent_event_feed(limit=6),
            "mongo_search_cache": self.mongo.search_cache_stats(limit=4),
            "mongo_geo_preview": {
                "anchor": "图书馆中心点",
                "items": self.mongo.nearby_spaces(116.39720, 39.90882, limit=5),
            },
            "mongo_gridfs_files": self.mongo.gridfs_files(limit=5),
            "leaderboard_catalog": self._leaderboard_catalog(reference_time),
            "rank_boards": self._book_rankboards(reference_time, limit=3),
            "course_rankings": self._course_rankings(limit=3),
            "practice_rankings": self._practice_progress_rankings(limit=3),
            "lab_rankings": self._lab_usage_rankings(limit=3),
            "rank_reference": reference_time.strftime("%Y-%m-%d %H:%M"),
            "graph_snapshot": graph_snapshot,
            "governance_teaser": {
                "quality_score": governance["quality_score_text"],
                "quality_label": governance["quality_label"],
                "master_entity_count": governance["master_entity_count"],
                "master_record_count": governance["master_record_count"],
                "snapshot_at": governance["snapshot_at"],
            },
        }
