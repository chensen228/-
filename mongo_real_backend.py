from __future__ import annotations

import mimetypes
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from bson import ObjectId
from gridfs import GridFS
from pymongo import ASCENDING, DESCENDING, GEOSPHERE, MongoClient


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def _normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    return value


class RealMongoStore:
    mode = "real"

    def __init__(self, uri: str, db_name: str) -> None:
        self.uri = uri
        self.db_name = db_name
        self.client = MongoClient(uri, serverSelectionTimeoutMS=1500)
        self.client.admin.command("ping")
        self.db = self.client[db_name]
        self.fs = GridFS(self.db)
        self.status_label = f"真实 MongoDB（{uri.replace('mongodb://', '')}/{db_name}）"
        self._structures_ready = False
        self.ensure_structures()

    def ensure_structures(self) -> None:
        if self._structures_ready:
            return

        validators = {
            "library_behavior_log": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["studentName", "action", "createdAt"],
                    "properties": {
                        "studentName": {"bsonType": "string"},
                        "action": {"bsonType": "string"},
                        "createdAt": {"bsonType": "date"},
                    },
                }
            },
            "teaching_change_log": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["operator", "changeType", "target", "createdAt"],
                    "properties": {
                        "operator": {"bsonType": "string"},
                        "changeType": {"bsonType": "string"},
                        "target": {"bsonType": "string"},
                        "createdAt": {"bsonType": "date"},
                    },
                }
            },
            "warning_profile": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["studentName", "riskType", "ruleHitList", "createdAt"],
                    "properties": {
                        "studentName": {"bsonType": "string"},
                        "riskType": {"bsonType": "string"},
                        "ruleHitList": {"bsonType": "array"},
                        "createdAt": {"bsonType": "date"},
                    },
                }
            },
            "internship_weekly_report": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["studentName", "weekNo", "content", "createdAt"],
                    "properties": {
                        "studentName": {"bsonType": "string"},
                        "content": {"bsonType": "string"},
                        "createdAt": {"bsonType": "date"},
                    },
                }
            },
            "evaluation_comment": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["studentName", "comment", "createdAt"],
                    "properties": {
                        "studentName": {"bsonType": "string"},
                        "comment": {"bsonType": "string"},
                        "createdAt": {"bsonType": "date"},
                    },
                }
            },
            "practice_risk_profile": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["studentName", "projectTitle", "riskLevel", "riskScore", "ruleHitList", "createdAt"],
                    "properties": {
                        "studentName": {"bsonType": "string"},
                        "projectTitle": {"bsonType": "string"},
                        "riskLevel": {"bsonType": "string"},
                        "riskScore": {"bsonType": ["int", "long", "double", "decimal"]},
                        "ruleHitList": {"bsonType": "array"},
                        "suggestionList": {"bsonType": "array"},
                        "createdAt": {"bsonType": "date"},
                    },
                }
            },
            "realtime_event_feed": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["eventType", "message", "createdAt"],
                    "properties": {
                        "eventType": {"bsonType": "string"},
                        "message": {"bsonType": "string"},
                        "createdAt": {"bsonType": "date"},
                    },
                }
            },
            "search_session_cache": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["keyword", "source", "createdAt", "expireAt"],
                    "properties": {
                        "keyword": {"bsonType": "string"},
                        "source": {"bsonType": "string"},
                        "createdAt": {"bsonType": "date"},
                        "expireAt": {"bsonType": "date"},
                    },
                }
            },
            "campus_space_geo": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["spaceCode", "spaceName", "spaceType", "location", "updatedAt"],
                    "properties": {
                        "spaceCode": {"bsonType": "string"},
                        "spaceName": {"bsonType": "string"},
                        "spaceType": {"bsonType": "string"},
                        "location": {"bsonType": "object"},
                        "updatedAt": {"bsonType": "date"},
                    },
                }
            },
            "data_quality_snapshot": {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": ["snapshotName", "summary", "totalScore", "snapshotDate", "createdAt"],
                    "properties": {
                        "snapshotName": {"bsonType": "string"},
                        "summary": {"bsonType": "string"},
                        "totalScore": {"bsonType": ["int", "long", "double", "decimal"]},
                        "snapshotDate": {"bsonType": "string"},
                        "dimensions": {"bsonType": "array"},
                        "masterEntities": {"bsonType": "array"},
                        "createdAt": {"bsonType": "date"},
                    },
                }
            },
        }

        for name in [
            "library_behavior_log",
            "teaching_change_log",
            "warning_profile",
            "internship_weekly_report",
            "evaluation_comment",
            "practice_risk_profile",
            "search_session_cache",
            "campus_space_geo",
            "data_quality_snapshot",
        ]:
            self._ensure_collection(name, validators[name])
        self._ensure_collection("realtime_event_feed", validators["realtime_event_feed"], capped=True, size=524288)

        self.db["library_behavior_log"].create_index([("createdAt", DESCENDING)])
        self.db["library_behavior_log"].create_index([("studentName", ASCENDING), ("action", ASCENDING)])
        self.db["teaching_change_log"].create_index([("createdAt", DESCENDING)])
        self.db["warning_profile"].create_index([("studentName", ASCENDING)])
        self.db["internship_weekly_report"].create_index([("studentName", ASCENDING), ("weekNo", ASCENDING)])
        self.db["internship_weekly_report"].create_index([("createdAt", DESCENDING)])
        self.db["evaluation_comment"].create_index([("studentName", ASCENDING)])
        self.db["evaluation_comment"].create_index([("createdAt", DESCENDING)])
        self.db["practice_risk_profile"].create_index([("studentName", ASCENDING)])
        self.db["practice_risk_profile"].create_index([("riskLevel", ASCENDING), ("riskScore", DESCENDING)])
        self.db["realtime_event_feed"].create_index([("createdAt", DESCENDING)])
        self.db["search_session_cache"].create_index([("keyword", ASCENDING)])
        self.db["search_session_cache"].create_index([("expireAt", ASCENDING)], expireAfterSeconds=0)
        self.db["campus_space_geo"].create_index([("spaceCode", ASCENDING)], unique=True)
        self.db["campus_space_geo"].create_index([("spaceType", ASCENDING)])
        self.db["campus_space_geo"].create_index([("location", GEOSPHERE)])
        self.db["data_quality_snapshot"].create_index([("createdAt", DESCENDING)])
        self.db["data_quality_snapshot"].create_index([("snapshotName", ASCENDING)])

        self._structures_ready = True

    def _ensure_collection(self, name: str, validator: dict[str, Any], capped: bool = False, size: int | None = None) -> None:
        if name not in self.db.list_collection_names():
            kwargs: dict[str, Any] = {"validator": validator, "validationLevel": "moderate"}
            if capped:
                kwargs["capped"] = True
                kwargs["size"] = size or 524288
            self.db.create_collection(name, **kwargs)
            return
        try:
            self.db.command(
                {
                    "collMod": name,
                    "validator": validator,
                    "validationLevel": "moderate",
                }
            )
        except Exception:
            pass

    def reset(self) -> None:
        self.client.drop_database(self.db_name)
        self.db = self.client[self.db_name]
        self.fs = GridFS(self.db)
        self._structures_ready = False
        self.ensure_structures()

    def read_all(self, collection: str) -> list[dict[str, Any]]:
        self.ensure_structures()
        rows = self.db[collection].find().sort("createdAt", ASCENDING)
        return [_normalize_value(dict(row)) for row in rows]

    def write_all(self, collection: str, documents: list[dict[str, Any]]) -> None:
        self.ensure_structures()
        self.db[collection].delete_many({})
        if documents:
            prepared = [self._prepare_document(document) for document in documents]
            self.db[collection].insert_many(prepared)

    def insert(self, collection: str, document: dict[str, Any]) -> None:
        self.ensure_structures()
        prepared = self._prepare_document(document)
        self.db[collection].insert_one(prepared)

    def recent(self, collection: str, limit: int = 6) -> list[dict[str, Any]]:
        self.ensure_structures()
        rows = self.db[collection].find().sort("createdAt", DESCENDING).limit(limit)
        return [_normalize_value(dict(row)) for row in rows]

    def count(self, collection: str) -> int:
        self.ensure_structures()
        return int(self.db[collection].count_documents({}))

    def _prepare_document(self, document: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(document)
        created_at = _parse_datetime(prepared.get("createdAt")) or datetime.now()
        prepared["createdAt"] = created_at
        return prepared

    def insert_event_feed(self, event_type: str, message: str, context: dict[str, Any] | None = None) -> None:
        payload = {"eventType": event_type, "message": message, "context": context or {}, "createdAt": datetime.now()}
        self.db["realtime_event_feed"].insert_one(payload)

    def recent_event_feed(self, limit: int = 6) -> list[dict[str, Any]]:
        rows = self.db["realtime_event_feed"].find().sort("createdAt", DESCENDING).limit(limit)
        return [_normalize_value(dict(row)) for row in rows]

    def cache_search(self, keyword: str, source: str = "web", ttl_minutes: int = 90) -> None:
        now = datetime.now()
        self.db["search_session_cache"].insert_one(
            {
                "keyword": keyword,
                "source": source,
                "createdAt": now,
                "expireAt": now + timedelta(minutes=ttl_minutes),
            }
        )

    def search_cache_stats(self, limit: int = 4) -> dict[str, Any]:
        rows = self.db["search_session_cache"].find().sort("createdAt", DESCENDING).limit(limit)
        return {
            "count": int(self.db["search_session_cache"].count_documents({})),
            "recent": [_normalize_value(dict(row)) for row in rows],
        }

    def upsert_geo_spaces(self, spaces: Iterable[dict[str, Any]]) -> None:
        self.ensure_structures()
        for space in spaces:
            payload = dict(space)
            payload["updatedAt"] = datetime.now()
            self.db["campus_space_geo"].replace_one({"spaceCode": payload["spaceCode"]}, payload, upsert=True)

    def nearby_spaces(self, lng: float, lat: float, limit: int = 5, space_type: str | None = None) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "location": {
                "$near": {
                    "$geometry": {"type": "Point", "coordinates": [lng, lat]},
                }
            }
        }
        if space_type:
            query["spaceType"] = space_type
        rows = self.db["campus_space_geo"].find(query).limit(limit)
        return [_normalize_value(dict(row)) for row in rows]

    def gridfs_sync_assets(self, asset_paths: Iterable[Path]) -> None:
        self.ensure_structures()
        for path in asset_paths:
            if not path.exists() or not path.is_file():
                continue
            existing = self.db["fs.files"].find_one({"metadata.sourcePath": str(path)})
            if existing:
                continue
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.fs.put(
                path.read_bytes(),
                filename=path.name,
                contentType=content_type,
                metadata={
                    "sourcePath": str(path),
                    "category": "smart-campus-demo",
                    "size": path.stat().st_size,
                },
            )

    def gridfs_files(self, limit: int = 6) -> list[dict[str, Any]]:
        rows = self.db["fs.files"].find().sort("uploadDate", DESCENDING).limit(limit)
        return [_normalize_value(dict(row)) for row in rows]

    def collection_catalog(self) -> list[dict[str, Any]]:
        self.ensure_structures()
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
        catalog: list[dict[str, Any]] = []
        for name in names:
            collection_info = next(self.db.list_collections(filter={"name": name}), {})
            options = collection_info.get("options", {})
            indexes = list(self.db[name].list_indexes())
            catalog.append(
                {
                    "name": name,
                    "count": int(self.db[name].count_documents({})),
                    "capped": bool(options.get("capped")),
                    "validator": "JSON Schema" if options.get("validator") else "none",
                    "indexes": [index["name"] for index in indexes],
                    "ttl_indexes": [index["name"] for index in indexes if "expireAfterSeconds" in index],
                }
            )
        return catalog

    def aggregation_showcase(self) -> dict[str, Any]:
        self.ensure_structures()
        library_actions = list(
            self.db["library_behavior_log"].aggregate(
                [
                    {"$group": {"_id": "$action", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1, "_id": 1}},
                ]
            )
        )
        space_types = list(
            self.db["campus_space_geo"].aggregate(
                [
                    {"$group": {"_id": "$spaceType", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1, "_id": 1}},
                ]
            )
        )
        event_types = list(
            self.db["realtime_event_feed"].aggregate(
                [
                    {"$group": {"_id": "$eventType", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1, "_id": 1}},
                ]
            )
        )
        return {
            "library_actions": [{"name": item.get("_id") or "unknown", "count": int(item["count"])} for item in library_actions],
            "space_types": [{"name": item.get("_id") or "unknown", "count": int(item["count"])} for item in space_types],
            "event_types": [{"name": item.get("_id") or "unknown", "count": int(item["count"])} for item in event_types],
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
