"""Read human chat names from Codex's local metadata, never its messages."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


class ThreadCatalog:
    def __init__(self, home: Path):
        self.home = home

    def read(self) -> dict[str, dict]:
        names = {}
        try:
            with (self.home / "session_index.jsonl").open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("thread_name"), str):
                        names[row["id"]] = {"title": row["thread_name"]}
        except OSError:
            pass
        databases = sorted(self.home.glob("state_*.sqlite"), key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1, reverse=True)
        for database in databases:
            try:
                with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)) as conn:
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
                    wanted = [c for c in ("id", "name", "title", "cwd", "source", "archived", "rollout_path") if c in columns]
                    if "id" not in wanted:
                        continue
                    for values in conn.execute("SELECT " + ",".join(wanted) + " FROM threads"):
                        row = dict(zip(wanted, values))
                        name = row.get("name") or names.get(row["id"], {}).get("title") or row.get("title")
                        if isinstance(name, str) and name.strip():
                            names[row["id"]] = {"title": name.strip(), "project": row.get("cwd"), "rollout_path": row.get("rollout_path")}
                break
            except (sqlite3.Error, OSError):
                continue
        return names


def chat_labels(sessions: list[dict]) -> list[str]:
    """Readable names + usage; disambiguate duplicate names without showing UUIDs."""
    titles = [s.get("title") or "未命名对话" for s in sessions]
    counts = {title: titles.count(title) for title in set(titles)}
    labels = []
    for index, session in enumerate(sessions):
        title = titles[index]
        if counts[title] > 1:
            project = Path(session.get("project") or "").name or "无项目"
            title += f" · {project} · {index + 1}"
        percent = session.get("percent")
        labels.append(f"{title}  ·  {percent:.1f}%" if percent is not None else f"{title}  ·  等待读数")
    return labels
