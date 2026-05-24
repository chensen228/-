from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import networkx as nx

try:
    from neo4j import GraphDatabase
except ImportError:
    GraphDatabase = None


def _escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


class CampusGraphService:
    def __init__(self, export_dir: Path) -> None:
        self.export_dir = export_dir
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.cypher_path = self.export_dir / "smart_campus_graph.cypher"
        runtime_config = self._load_runtime_config()
        if os.getenv("RENDER", "").lower() == "true" and not os.getenv("SMART_CAMPUS_NEO4J_URI"):
            runtime_config = {}
        self.uri = os.getenv("SMART_CAMPUS_NEO4J_URI", runtime_config.get("uri", ""))
        self.user = os.getenv("SMART_CAMPUS_NEO4J_USER", runtime_config.get("user", ""))
        self.password = os.getenv("SMART_CAMPUS_NEO4J_PASSWORD", runtime_config.get("password", ""))
        self.driver = None
        self.mode = "analysis"
        self.status_label = "本地图分析模式"
        self._last_export_signature = ""
        self._last_sync_signature = ""
        if GraphDatabase is not None and self.uri and self.user and self.password:
            try:
                driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
                with driver.session() as session:
                    session.run("RETURN 1").single()
                self.driver = driver
                self.mode = "neo4j"
                self.status_label = f"Neo4j 已连接（{self.uri}）"
            except Exception:
                self.driver = None
                self.mode = "analysis"
                self.status_label = "本地图分析模式"
        elif GraphDatabase is not None:
            self.status_label = "本地图分析模式"

    def runtime(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "status_label": self.status_label,
            "cypher_path": str(self.cypher_path),
            "cypher_name": self.cypher_path.name,
        }

    def _load_runtime_config(self) -> dict[str, str]:
        config_path = self.export_dir.parent / "neo4j_runtime.json"
        if not config_path.exists():
            return {}
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {
            "uri": str(payload.get("uri", "")).strip(),
            "user": str(payload.get("user", "")).strip(),
            "password": str(payload.get("password", "")).strip(),
        }

    def default_student_id(self, dataset: dict[str, Any], candidates: list[int]) -> int | None:
        graph = self._build_graph(dataset)
        scores: dict[int, int] = {}
        for student_id in candidates:
            student_key = f"student:{student_id}"
            if student_key not in graph:
                continue
            score = (
                len(self._course_recommendations(graph, student_key)) * 5
                + len(self._book_recommendations(graph, student_key)) * 3
                + len(self._path_showcase(graph, student_key))
            )
            scores[student_id] = score
        if not scores:
            return None
        return max(scores, key=lambda student_id: (scores[student_id], -student_id))

    def overview(self, dataset: dict[str, Any], selected_student_id: int | None = None) -> dict[str, Any]:
        graph = self._build_graph(dataset)
        selected_student = f"student:{selected_student_id}" if selected_student_id else None
        export_signature = self._export_cypher(dataset)
        if self.driver is not None:
            self._sync_to_neo4j(export_signature)
        return {
            "node_count": graph.number_of_nodes(),
            "edge_count": graph.number_of_edges(),
            "node_summary": self._count_labels(graph),
            "edge_summary": self._count_edge_types(graph),
            "top_nodes": self._top_nodes(graph),
            "centrality_ranking": self._centrality_ranking(graph),
            "student_link_index": self._student_link_index(graph),
            "course_recommendations": self._course_recommendations(graph, selected_student),
            "book_recommendations": self._book_recommendations(graph, selected_student),
            "path_showcase": self._path_showcase(graph, selected_student),
            "cypher_examples": self._cypher_examples(graph, selected_student),
            "cypher_path": str(self.cypher_path),
            "cypher_name": self.cypher_path.name,
            "network_data": self._generate_vis_network(graph),
        }

    def _generate_vis_network(self, graph: nx.MultiDiGraph) -> dict[str, list[dict[str, Any]]]:
        nodes = []
        edges = []
        for node_id, attrs in graph.nodes(data=True):
            group = attrs.get("label", "Unknown")
            detail_map = {k: v for k, v in attrs.items() if k not in ("label", "name")}
            title = "<br>".join([f"<b>{k}</b>: {v}" for k, v in detail_map.items()])
            nodes.append({
                "id": node_id,
                "label": attrs.get("name", node_id),
                "group": group,
                "title": f"<b>{group}</b><br>{title}" if title else f"<b>{group}</b>",
                "entityLabel": group,
                "detailMap": detail_map,
                "searchText": f"{attrs.get('name', '')} {group}".lower(),
            })
        for index, (source, target, attrs) in enumerate(graph.edges(data=True), start=1):
            detail_map = {k: v for k, v in attrs.items() if k != "type"}
            title = "<br>".join([f"<b>{k}</b>: {v}" for k, v in detail_map.items()])
            edges.append({
                "id": f"edge-{index}",
                "from": source,
                "to": target,
                "label": attrs.get("type", ""),
                "title": title if title else attrs.get("type", ""),
                "arrows": "to",
                "relationType": attrs.get("type", ""),
                "detailMap": detail_map,
            })
        return {"nodes": nodes, "edges": edges}

    def _build_graph(self, dataset: dict[str, Any]) -> nx.MultiDiGraph:
        graph = nx.MultiDiGraph()

        for student in dataset["students"]:
            student_key = f"student:{student['student_id']}"
            major_key = f"major:{student['major']}"
            college_key = f"college:{student['college']}"
            graph.add_node(student_key, label="Student", name=student["student_name"], student_no=student["student_no"])
            graph.add_node(major_key, label="Major", name=student["major"])
            graph.add_node(college_key, label="College", name=student["college"])
            graph.add_edge(student_key, major_key, type="MAJOR_IN")
            graph.add_edge(major_key, college_key, type="BELONGS_TO")

        for teacher in dataset["teachers"]:
            graph.add_node(
                f"teacher:{teacher['teacher_id']}",
                label="Teacher",
                name=teacher["teacher_name"],
                teacher_no=teacher["teacher_no"],
            )

        for course in dataset["courses"]:
            graph.add_node(
                f"course:{course['course_id']}",
                label="Course",
                name=course["course_name"],
                course_code=course["course_code"],
            )

        for offering in dataset["offerings"]:
            graph.add_edge(
                f"teacher:{offering['teacher_id']}",
                f"course:{offering['course_id']}",
                type="TEACHES",
                term=offering["term"],
                classroom=offering["classroom"],
            )

        for selection in dataset["selections"]:
            graph.add_edge(
                f"student:{selection['student_id']}",
                f"course:{selection['course_id']}",
                type="ENROLLED_IN",
                selected_at=selection["selected_at"],
                status=selection["status"],
            )

        for score in dataset["scores"]:
            graph.add_edge(
                f"student:{score['student_id']}",
                f"course:{score['course_id']}",
                type="SCORED_IN",
                total_score=score["total_score"],
            )

        for book in dataset["books"]:
            graph.add_node(f"book:{book['book_id']}", label="Book", name=book["title"], isbn=book["isbn"])
            graph.add_node(f"category:{book['category']}", label="Category", name=book["category"])
            graph.add_edge(f"book:{book['book_id']}", f"category:{book['category']}", type="IN_CATEGORY")

        for borrowing in dataset["borrowings"]:
            graph.add_edge(
                f"student:{borrowing['student_id']}",
                f"book:{borrowing['book_id']}",
                type="BORROWED",
                status=borrowing["status"],
            )

        for project in dataset["projects"]:
            graph.add_node(f"project:{project['project_id']}", label="Project", name=project["project_name"])

        for room in dataset["lab_rooms"]:
            graph.add_node(f"lab:{room['room_id']}", label="LabRoom", name=room["room_name"])

        for task in dataset["tasks"]:
            task_key = f"task:{task['task_id']}"
            base_key = f"base:{task['base_name']}"
            mentor_key = f"mentor:{task['mentor_name']}"
            graph.add_node(task_key, label="Task", name=task["project_title"])
            graph.add_node(base_key, label="Base", name=task["base_name"])
            graph.add_node(mentor_key, label="Mentor", name=task["mentor_name"])
            graph.add_edge(f"student:{task['student_id']}", task_key, type="HAS_TASK", progress=task["progress"])
            graph.add_edge(task_key, base_key, type="AT_BASE")
            graph.add_edge(task_key, mentor_key, type="GUIDED_BY")

        for booking in dataset["lab_bookings"]:
            student_key = f"student:{booking['student_id']}"
            lab_key = f"lab:{booking['room_id']}"
            project_key = f"project:{booking['project_id']}"
            graph.add_edge(student_key, lab_key, type="BOOKED_LAB", booking_date=booking["booking_date"])
            graph.add_edge(lab_key, project_key, type="SERVES_PROJECT")

        return graph

    def _count_labels(self, graph: nx.MultiDiGraph) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for _, attrs in graph.nodes(data=True):
            counts[attrs["label"]] = counts.get(attrs["label"], 0) + 1
        return [{"label": label, "count": count} for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]

    def _count_edge_types(self, graph: nx.MultiDiGraph) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for _, _, attrs in graph.edges(data=True):
            counts[attrs["type"]] = counts.get(attrs["type"], 0) + 1
        return [{"label": label, "count": count} for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]

    def _top_nodes(self, graph: nx.MultiDiGraph, limit: int = 6) -> list[dict[str, Any]]:
        rows = sorted(graph.degree(), key=lambda item: item[1], reverse=True)[:limit]
        return [
            {
                "name": graph.nodes[node]["name"],
                "label": graph.nodes[node]["label"],
                "degree": degree,
            }
            for node, degree in rows
        ]

    def _project_graph(self, graph: nx.MultiDiGraph) -> nx.DiGraph:
        projected = nx.DiGraph()
        for node, attrs in graph.nodes(data=True):
            projected.add_node(node, **attrs)
        for source, target in graph.edges():
            if projected.has_edge(source, target):
                projected[source][target]["weight"] += 1
            else:
                projected.add_edge(source, target, weight=1)
        return projected

    def _centrality_ranking(self, graph: nx.MultiDiGraph, limit: int = 6) -> list[dict[str, Any]]:
        projected = self._project_graph(graph)
        if projected.number_of_edges() == 0:
            return []
        scores = nx.pagerank(projected, weight="weight")
        degrees = dict(graph.degree())
        rows = sorted(scores.items(), key=lambda item: (-item[1], -degrees.get(item[0], 0), graph.nodes[item[0]]["name"]))[:limit]
        return [
            {
                "name": graph.nodes[node]["name"],
                "label": graph.nodes[node]["label"],
                "score": round(score, 4),
                "degree": degrees.get(node, 0),
            }
            for node, score in rows
        ]

    def _student_link_index(self, graph: nx.MultiDiGraph, limit: int = 5) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for node, attrs in graph.nodes(data=True):
            if attrs["label"] != "Student":
                continue
            academic_links = len(
                {
                    target
                    for _, target, edge_attrs in graph.out_edges(node, data=True)
                    if edge_attrs["type"] in {"ENROLLED_IN", "SCORED_IN"}
                }
            )
            library_links = len(
                {
                    target
                    for _, target, edge_attrs in graph.out_edges(node, data=True)
                    if edge_attrs["type"] == "BORROWED"
                }
            )
            practice_links = len(
                {
                    target
                    for _, target, edge_attrs in graph.out_edges(node, data=True)
                    if edge_attrs["type"] in {"HAS_TASK", "BOOKED_LAB"}
                }
            )
            domains = sum(1 for value in [academic_links, library_links, practice_links] if value)
            score = domains * 10 + academic_links * 2 + library_links * 2 + practice_links * 3
            rows.append(
                {
                    "name": attrs["name"],
                    "student_no": attrs["student_no"],
                    "domains": domains,
                    "academic_links": academic_links,
                    "library_links": library_links,
                    "practice_links": practice_links,
                    "score": score,
                }
            )
        return sorted(rows, key=lambda item: (-item["domains"], -item["score"], item["name"]))[:limit]

    def _course_recommendations(self, graph: nx.MultiDiGraph, student_key: str | None) -> list[dict[str, Any]]:
        if not student_key or student_key not in graph:
            return []
        student_courses = {
            target
            for _, target, attrs in graph.out_edges(student_key, data=True)
            if attrs["type"] == "ENROLLED_IN"
        }
        if not student_courses:
            return []

        candidates: dict[str, dict[str, Any]] = {}
        for node, attrs in graph.nodes(data=True):
            if attrs["label"] != "Student" or node == student_key:
                continue
            peer_courses = {
                target
                for _, target, edge_attrs in graph.out_edges(node, data=True)
                if edge_attrs["type"] == "ENROLLED_IN"
            }
            overlap = student_courses & peer_courses
            if not overlap:
                continue
            shared_strength = len(overlap)
            for course in peer_courses - student_courses:
                candidate = candidates.setdefault(
                    course,
                    {
                        "name": graph.nodes[course]["name"],
                        "score": 0,
                        "support": set(),
                        "path": None,
                    },
                )
                candidate["score"] += shared_strength
                candidate["support"].add(graph.nodes[node]["name"])
                if candidate["path"] is None:
                    shared_course = graph.nodes[next(iter(overlap))]["name"]
                    candidate["path"] = f"{graph.nodes[student_key]['name']} -> {shared_course} <- {graph.nodes[node]['name']} -> {graph.nodes[course]['name']}"

        ordered = sorted(candidates.values(), key=lambda item: (-item["score"], item["name"]))
        return [
            {
                "name": item["name"],
                "score": item["score"],
                "support": "、".join(sorted(item["support"])),
                "path": item["path"],
            }
            for item in ordered[:5]
        ]

    def _book_recommendations(self, graph: nx.MultiDiGraph, student_key: str | None) -> list[dict[str, Any]]:
        if not student_key or student_key not in graph:
            return []
        borrowed_books = {
            target
            for _, target, attrs in graph.out_edges(student_key, data=True)
            if attrs["type"] == "BORROWED"
        }
        if not borrowed_books:
            return []
        preferred_categories = {
            target
            for book in borrowed_books
            for _, target, attrs in graph.out_edges(book, data=True)
            if attrs["type"] == "IN_CATEGORY"
        }
        candidates: list[dict[str, Any]] = []
        for category in preferred_categories:
            for source, _, attrs in graph.in_edges(category, data=True):
                if attrs["type"] != "IN_CATEGORY" or source in borrowed_books:
                    continue
                popularity = sum(
                    1
                    for _, _, borrow_attrs in graph.in_edges(source, data=True)
                    if borrow_attrs["type"] == "BORROWED"
                )
                candidates.append(
                    {
                        "name": graph.nodes[source]["name"],
                        "category": graph.nodes[category]["name"],
                        "score": popularity,
                    }
                )
        dedup: dict[str, dict[str, Any]] = {}
        for item in candidates:
            current = dedup.get(item["name"])
            if current is None or item["score"] > current["score"]:
                dedup[item["name"]] = item
        return sorted(dedup.values(), key=lambda item: (-item["score"], item["name"]))[:5]

    def _path_showcase(self, graph: nx.MultiDiGraph, student_key: str | None) -> list[dict[str, str]]:
        if not student_key or student_key not in graph:
            return []
        student_name = graph.nodes[student_key]["name"]
        items: list[dict[str, str]] = []
        for _, task_key, attrs in graph.out_edges(student_key, data=True):
            if attrs["type"] != "HAS_TASK":
                continue
            task_name = graph.nodes[task_key]["name"]
            for _, base_key, edge_attrs in graph.out_edges(task_key, data=True):
                if edge_attrs["type"] == "AT_BASE":
                    items.append({"title": "实践路径", "detail": f"{student_name} -> {task_name} -> {graph.nodes[base_key]['name']}"})
            for _, mentor_key, edge_attrs in graph.out_edges(task_key, data=True):
                if edge_attrs["type"] == "GUIDED_BY":
                    items.append({"title": "导师路径", "detail": f"{student_name} -> {task_name} -> {graph.nodes[mentor_key]['name']}"})
        course_paths = self._course_recommendations(graph, student_key)
        if course_paths:
            items.append({"title": "选课推荐路径", "detail": course_paths[0]["path"]})
        return items[:6]

    def _cypher_examples(self, graph: nx.MultiDiGraph, student_key: str | None) -> list[str]:
        if not student_key or student_key not in graph:
            return []
        student_no = graph.nodes[student_key]["student_no"]
        return [
            (
                "MATCH (s:Student {studentNo: '%s'})-[:ENROLLED_IN]->(c:Course)<-[:ENROLLED_IN]-(peer:Student)-[:ENROLLED_IN]->(rec:Course) "
                "WHERE NOT (s)-[:ENROLLED_IN]->(rec) RETURN rec.courseName, count(DISTINCT peer) AS support ORDER BY support DESC LIMIT 5"
            )
            % student_no,
            (
                "MATCH (s:Student {studentNo: '%s'})-[:BORROWED]->(:Book)-[:IN_CATEGORY]->(cat:Category)<-[:IN_CATEGORY]-(book:Book) "
                "WHERE NOT (s)-[:BORROWED]->(book) RETURN cat.name, book.title LIMIT 5"
            )
            % student_no,
            (
                "MATCH p = (s:Student {studentNo: '%s'})-[:HAS_TASK]->(:Task)-[:GUIDED_BY|AT_BASE]->() RETURN p LIMIT 5"
            )
            % student_no,
        ]

    def _export_cypher(self, dataset: dict[str, Any]) -> str:
        lines: list[str] = ["MATCH (n) DETACH DELETE n;"]

        for student in dataset["students"]:
            lines.append(
                "MERGE (:Student {studentNo: '%s', studentName: '%s', major: '%s', college: '%s'});"
                % (
                    _escape(student["student_no"]),
                    _escape(student["student_name"]),
                    _escape(student["major"]),
                    _escape(student["college"]),
                )
            )
            lines.append("MERGE (:Major {name: '%s'});" % _escape(student["major"]))
            lines.append("MERGE (:College {name: '%s'});" % _escape(student["college"]))
            lines.append(
                "MATCH (s:Student {studentNo: '%s'}), (m:Major {name: '%s'}) MERGE (s)-[:MAJOR_IN]->(m);"
                % (_escape(student["student_no"]), _escape(student["major"]))
            )
            lines.append(
                "MATCH (m:Major {name: '%s'}), (c:College {name: '%s'}) MERGE (m)-[:BELONGS_TO]->(c);"
                % (_escape(student["major"]), _escape(student["college"]))
            )

        for teacher in dataset["teachers"]:
            lines.append(
                "MERGE (:Teacher {teacherNo: '%s', teacherName: '%s'});"
                % (_escape(teacher["teacher_no"]), _escape(teacher["teacher_name"]))
            )

        for course in dataset["courses"]:
            lines.append(
                "MERGE (:Course {courseCode: '%s', courseName: '%s'});"
                % (_escape(course["course_code"]), _escape(course["course_name"]))
            )

        for offering in dataset["offerings"]:
            lines.append(
                "MATCH (t:Teacher {teacherNo: '%s'}), (c:Course {courseCode: '%s'}) MERGE (t)-[:TEACHES {term: '%s', classroom: '%s'}]->(c);"
                % (
                    _escape(offering["teacher_no"]),
                    _escape(offering["course_code"]),
                    _escape(offering["term"]),
                    _escape(offering["classroom"]),
                )
            )

        for selection in dataset["selections"]:
            lines.append(
                "MATCH (s:Student {studentNo: '%s'}), (c:Course {courseCode: '%s'}) MERGE (s)-[:ENROLLED_IN {status: '%s'}]->(c);"
                % (
                    _escape(selection["student_no"]),
                    _escape(selection["course_code"]),
                    _escape(selection["status"]),
                )
            )

        for book in dataset["books"]:
            lines.append(
                "MERGE (:Book {isbn: '%s', title: '%s'});"
                % (_escape(book["isbn"]), _escape(book["title"]))
            )
            lines.append("MERGE (:Category {name: '%s'});" % _escape(book["category"]))
            lines.append(
                "MATCH (b:Book {isbn: '%s'}), (c:Category {name: '%s'}) MERGE (b)-[:IN_CATEGORY]->(c);"
                % (_escape(book["isbn"]), _escape(book["category"]))
            )

        for borrowing in dataset["borrowings"]:
            lines.append(
                "MATCH (s:Student {studentNo: '%s'}), (b:Book {isbn: '%s'}) MERGE (s)-[:BORROWED {status: '%s'}]->(b);"
                % (
                    _escape(borrowing["student_no"]),
                    _escape(borrowing["isbn"]),
                    _escape(borrowing["status"]),
                )
            )

        for room in dataset["lab_rooms"]:
            lines.append("MERGE (:LabRoom {roomCode: '%s', roomName: '%s'});" % (_escape(str(room["room_id"])), _escape(room["room_name"])))

        for project in dataset["projects"]:
            lines.append("MERGE (:Project {name: '%s'});" % _escape(project["project_name"]))

        for task in dataset["tasks"]:
            lines.append("MERGE (:Base {name: '%s'});" % _escape(task["base_name"]))
            lines.append("MERGE (:Mentor {name: '%s'});" % _escape(task["mentor_name"]))
            lines.append("MERGE (:Task {taskCode: '%s', title: '%s'});" % (_escape(str(task["task_id"])), _escape(task["project_title"])))
            lines.append(
                "MATCH (s:Student {studentNo: '%s'}), (t:Task {taskCode: '%s'}) MERGE (s)-[:HAS_TASK {progress: %s}]->(t);"
                % (_escape(task["student_no"]), _escape(str(task["task_id"])), task["progress"])
            )
            lines.append(
                "MATCH (t:Task {taskCode: '%s'}), (b:Base {name: '%s'}) MERGE (t)-[:AT_BASE]->(b);"
                % (_escape(str(task["task_id"])), _escape(task["base_name"]))
            )
            lines.append(
                "MATCH (t:Task {taskCode: '%s'}), (m:Mentor {name: '%s'}) MERGE (t)-[:GUIDED_BY]->(m);"
                % (_escape(str(task["task_id"])), _escape(task["mentor_name"]))
            )

        for booking in dataset["lab_bookings"]:
            lines.append(
                "MATCH (s:Student {studentNo: '%s'}), (l:LabRoom {roomCode: '%s'}) MERGE (s)-[:BOOKED_LAB {bookingDate: '%s'}]->(l);"
                % (
                    _escape(booking["student_no"]),
                    _escape(str(booking["room_id"])),
                    _escape(booking["booking_date"]),
                )
            )
            lines.append(
                "MATCH (l:LabRoom {roomCode: '%s'}), (p:Project {name: '%s'}) MERGE (l)-[:SERVES_PROJECT]->(p);"
                % (_escape(str(booking["room_id"])), _escape(booking["project_name"]))
            )

        script = "\n".join(lines)
        signature = hashlib.sha256(script.encode("utf-8")).hexdigest()
        if signature != self._last_export_signature or not self.cypher_path.exists():
            self.cypher_path.write_text(script, encoding="utf-8")
            self._last_export_signature = signature
        return signature

    def _sync_to_neo4j(self, export_signature: str) -> None:
        if self.driver is None or export_signature == self._last_sync_signature:
            return
        script = self.cypher_path.read_text(encoding="utf-8")
        statements = [statement.strip() for statement in script.split(";") if statement.strip()]
        with self.driver.session() as session:
            for statement in statements:
                session.run(statement)
        self._last_sync_signature = export_signature
