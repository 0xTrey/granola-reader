"""
Granola local cache reader.

Reads meeting notes, transcripts, and panels directly from Granola's
local Electron cache at ~/Library/Application Support/Granola/cache-v3.json.

Usage as module:
    from granola_reader import GranolaReader
    gr = GranolaReader()
    meetings = gr.get_meetings(days=7)
    notes = gr.get_notes(doc_id)
    transcript = gr.get_transcript(doc_id)

Usage as CLI:
    python granola_reader.py meetings --days 7
    python granola_reader.py notes <doc_id>
    python granola_reader.py transcript <doc_id>
    python granola_reader.py search "keyword"
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional


CACHE_PATH = Path.home() / "Library" / "Application Support" / "Granola" / "cache-v3.json"


class _HTMLToMarkdown(HTMLParser):
    """Minimal HTML-to-markdown converter for Granola panel content."""

    def __init__(self):
        super().__init__()
        self._output: list[str] = []
        self._list_depth = 0
        self._in_li = False
        self._heading_level = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("ul", "ol"):
            self._list_depth += 1
        elif tag == "li":
            self._in_li = True
            indent = "  " * (self._list_depth - 1)
            self._output.append(f"\n{indent}- ")
        elif tag == "br":
            self._output.append("\n")
        elif tag == "p":
            self._output.append("\n\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self._heading_level = level
            self._output.append(f"\n\n{'#' * level} ")
        elif tag == "strong" or tag == "b":
            self._output.append("**")
        elif tag == "em" or tag == "i":
            self._output.append("*")
        elif tag == "a":
            href = dict(attrs).get("href", "")
            self._output.append(f"[")
            self._link_href = href

    def handle_endtag(self, tag):
        if tag in ("ul", "ol"):
            self._list_depth = max(0, self._list_depth - 1)
            if self._list_depth == 0:
                self._output.append("\n")
        elif tag == "li":
            self._in_li = False
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading_level = 0
            self._output.append("\n")
        elif tag == "strong" or tag == "b":
            self._output.append("**")
        elif tag == "em" or tag == "i":
            self._output.append("*")
        elif tag == "a":
            href = getattr(self, "_link_href", "")
            self._output.append(f"]({href})")

    def handle_data(self, data):
        self._output.append(data)

    def get_markdown(self) -> str:
        text = "".join(self._output)
        # Collapse excessive newlines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(html: str) -> str:
    """Convert HTML string to markdown."""
    parser = _HTMLToMarkdown()
    parser.feed(html)
    return parser.get_markdown()


class GranolaReader:
    """Read Granola meeting data from the local cache."""

    def __init__(self, cache_path: Optional[Path] = None):
        self._cache_path = cache_path or CACHE_PATH
        self._state: Optional[dict] = None

    def _load(self) -> dict:
        """Load and parse the cache file. Cached after first load."""
        if self._state is not None:
            return self._state

        if not self._cache_path.exists():
            raise FileNotFoundError(
                f"Granola cache not found at {self._cache_path}. "
                "Is Granola installed and has it been opened at least once?"
            )

        raw = json.loads(self._cache_path.read_text())
        self._state = json.loads(raw["cache"])["state"]
        return self._state

    def reload(self):
        """Force reload from disk (useful if Granola updated the cache)."""
        self._state = None
        self._load()

    def get_meetings(
        self,
        days: Optional[int] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """
        Get meetings from the cache.

        Args:
            days: Look back N days from today.
            since: Start date (YYYY-MM-DD). Overrides days.
            until: End date (YYYY-MM-DD). Defaults to today.
            limit: Max number of results.

        Returns list of dicts with keys:
            id, title, date, created_at, attendees, has_notes,
            has_transcript, valid_meeting
        """
        state = self._load()
        docs = state["documents"]
        panels = state.get("documentPanels", {})
        transcripts = state.get("transcripts", {})

        # Date filtering
        now = datetime.now(timezone.utc)
        if since:
            start = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        elif days:
            start = now - timedelta(days=days)
        else:
            start = datetime.min.replace(tzinfo=timezone.utc)

        if until:
            end = datetime.strptime(until, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
        else:
            end = now

        results = []
        for doc_id, doc in docs.items():
            if doc.get("deleted_at"):
                continue

            created = doc.get("created_at", "")
            if not created:
                continue

            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                continue

            if dt < start or dt > end:
                continue

            # Extract attendees from google_calendar_event or meetingsMetadata
            attendees = self._extract_attendees(doc, state)

            # Determine content availability
            has_notes = bool(doc.get("notes_markdown") or doc.get("notes_plain"))
            has_panel = bool(panels.get(doc_id))
            has_transcript = bool(transcripts.get(doc_id))

            # Get meeting start time from calendar event
            gce = doc.get("google_calendar_event") or {}
            start_time = ""
            if gce.get("start"):
                start_time = gce["start"].get("dateTime", gce["start"].get("date", ""))

            results.append({
                "id": doc_id,
                "title": doc.get("title", "Untitled"),
                "date": created[:10],
                "created_at": created,
                "start_time": start_time,
                "attendees": attendees,
                "has_notes": has_notes,
                "has_panel": has_panel,
                "has_transcript": has_transcript,
                "valid_meeting": doc.get("valid_meeting", False),
                "type": doc.get("type", ""),
            })

        # Sort by date descending
        results.sort(key=lambda m: m["created_at"], reverse=True)

        if limit:
            results = results[:limit]

        return results

    def get_notes(self, doc_id: str, format: str = "markdown") -> dict:
        """
        Get notes for a meeting.

        Args:
            doc_id: Document ID.
            format: "markdown", "plain", or "html".

        Returns dict with keys:
            title, notes, panels (list of {title, content}), user_notes
        """
        state = self._load()
        doc = state["documents"].get(doc_id)
        if not doc:
            raise KeyError(f"Document {doc_id} not found")

        result = {
            "id": doc_id,
            "title": doc.get("title", "Untitled"),
            "date": doc.get("created_at", "")[:10],
            "user_notes": "",
            "panels": [],
        }

        # User's own notes (typed during meeting)
        if format == "markdown" and doc.get("notes_markdown"):
            result["user_notes"] = doc["notes_markdown"]
        elif doc.get("notes_plain"):
            result["user_notes"] = doc["notes_plain"]

        # AI-generated panels (structured summaries)
        panels = state.get("documentPanels", {}).get(doc_id, {})
        for panel_id, panel in panels.items():
            if not isinstance(panel, dict):
                continue
            if panel.get("deleted_at"):
                continue

            panel_entry = {
                "id": panel_id,
                "title": panel.get("title", ""),
                "template": panel.get("template_slug", ""),
            }

            if format == "html":
                panel_entry["content"] = panel.get("original_content", "")
            elif format == "markdown":
                html = panel.get("original_content", "")
                if html:
                    panel_entry["content"] = html_to_markdown(html)
                else:
                    # Fall back to tiptap JSON content
                    panel_entry["content"] = self._tiptap_to_text(
                        panel.get("content", {})
                    )
            else:
                html = panel.get("original_content", "")
                if html:
                    # Strip tags for plain text
                    panel_entry["content"] = re.sub(r"<[^>]+>", "", html)
                else:
                    panel_entry["content"] = self._tiptap_to_text(
                        panel.get("content", {})
                    )

            result["panels"].append(panel_entry)

        return result

    def get_transcript(self, doc_id: str) -> dict:
        """
        Get the raw transcript for a meeting.

        Returns dict with keys:
            id, title, entries (list of {timestamp, source, text, speaker})
        """
        state = self._load()
        doc = state["documents"].get(doc_id)
        if not doc:
            raise KeyError(f"Document {doc_id} not found")

        raw_entries = state.get("transcripts", {}).get(doc_id, [])

        entries = []
        for entry in raw_entries:
            entries.append({
                "timestamp": entry.get("start_timestamp", ""),
                "end_timestamp": entry.get("end_timestamp", ""),
                "source": entry.get("source", "unknown"),
                "text": entry.get("text", ""),
                "speaker": entry.get("speaker_name", ""),
            })

        return {
            "id": doc_id,
            "title": doc.get("title", "Untitled"),
            "date": doc.get("created_at", "")[:10],
            "entry_count": len(entries),
            "entries": entries,
        }

    def search(
        self,
        query: str,
        days: Optional[int] = None,
        include_transcripts: bool = False,
        limit: int = 20,
    ) -> list[dict]:
        """
        Search meetings by keyword across titles, notes, panels, and optionally transcripts.

        Returns list of matching meetings with relevance context.
        """
        state = self._load()
        docs = state["documents"]
        panels = state.get("documentPanels", {})
        transcripts = state.get("transcripts", {})

        now = datetime.now(timezone.utc)
        cutoff = None
        if days:
            cutoff = now - timedelta(days=days)

        query_lower = query.lower()
        results = []

        for doc_id, doc in docs.items():
            if doc.get("deleted_at"):
                continue

            created = doc.get("created_at", "")
            if cutoff and created:
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if dt < cutoff:
                        continue
                except ValueError:
                    pass

            matches = []

            # Search title
            title = doc.get("title", "")
            if query_lower in title.lower():
                matches.append(f"title: {title}")

            # Search notes
            notes_text = doc.get("notes_plain") or doc.get("notes_markdown") or ""
            if query_lower in notes_text.lower():
                idx = notes_text.lower().index(query_lower)
                start = max(0, idx - 40)
                end = min(len(notes_text), idx + len(query) + 40)
                snippet = notes_text[start:end].replace("\n", " ")
                matches.append(f"notes: ...{snippet}...")

            # Search panels
            doc_panels = panels.get(doc_id, {})
            for panel in doc_panels.values():
                if not isinstance(panel, dict):
                    continue
                html = panel.get("original_content", "")
                plain = re.sub(r"<[^>]+>", "", html) if html else ""
                if query_lower in plain.lower():
                    idx = plain.lower().index(query_lower)
                    start = max(0, idx - 40)
                    end = min(len(plain), idx + len(query) + 40)
                    snippet = plain[start:end].replace("\n", " ")
                    panel_title = panel.get("title", "panel")
                    matches.append(f"{panel_title}: ...{snippet}...")

            # Search transcripts
            if include_transcripts:
                t_entries = transcripts.get(doc_id, [])
                for entry in t_entries:
                    text = entry.get("text", "")
                    if query_lower in text.lower():
                        matches.append(f"transcript: {text[:100]}")
                        break

            if matches:
                results.append({
                    "id": doc_id,
                    "title": title,
                    "date": created[:10],
                    "matches": matches,
                })

        results.sort(key=lambda r: r["date"], reverse=True)
        return results[:limit]

    def get_meeting_full(self, doc_id: str) -> dict:
        """
        Get everything for a single meeting: metadata, notes, panels, transcript.
        Convenience method combining get_notes and get_transcript.
        """
        state = self._load()
        doc = state["documents"].get(doc_id)
        if not doc:
            raise KeyError(f"Document {doc_id} not found")

        notes = self.get_notes(doc_id)
        transcript = self.get_transcript(doc_id)
        attendees = self._extract_attendees(doc, state)

        gce = doc.get("google_calendar_event") or {}

        return {
            "id": doc_id,
            "title": doc.get("title", "Untitled"),
            "date": doc.get("created_at", "")[:10],
            "created_at": doc.get("created_at", ""),
            "start_time": gce.get("start", {}).get("dateTime", ""),
            "end_time": gce.get("end", {}).get("dateTime", ""),
            "attendees": attendees,
            "user_notes": notes["user_notes"],
            "panels": notes["panels"],
            "transcript_entries": transcript["entry_count"],
            "transcript": transcript["entries"],
        }

    def _extract_attendees(self, doc: dict, state: dict) -> list[dict]:
        """Extract attendee info from calendar event or meetings metadata."""
        attendees = []

        # Try google_calendar_event first
        gce = doc.get("google_calendar_event") or {}
        if gce.get("attendees"):
            for a in gce["attendees"]:
                attendees.append({
                    "name": a.get("displayName", ""),
                    "email": a.get("email", ""),
                })
            return attendees

        # Fall back to meetingsMetadata
        mm = state.get("meetingsMetadata", {}).get(doc.get("id", ""), {})
        if isinstance(mm, dict) and mm.get("attendees"):
            for a in mm["attendees"]:
                attendees.append({
                    "name": a.get("name", ""),
                    "email": a.get("email", ""),
                })

        return attendees

    def _tiptap_to_text(self, tiptap: dict) -> str:
        """Extract plain text from tiptap JSON content structure."""
        if not tiptap or not isinstance(tiptap, dict):
            return ""

        parts = []
        self._walk_tiptap(tiptap, parts)
        return "\n".join(parts)

    def _walk_tiptap(self, node: dict, parts: list[str], depth: int = 0):
        """Recursively walk tiptap JSON and extract text."""
        node_type = node.get("type", "")

        if node_type == "text":
            parts.append(node.get("text", ""))
            return

        if node_type == "heading":
            level = node.get("attrs", {}).get("level", 1)
            prefix = "#" * level + " "
            child_text = []
            for child in node.get("content", []):
                self._walk_tiptap(child, child_text, depth)
            parts.append(prefix + "".join(child_text))
            return

        if node_type == "listItem":
            indent = "  " * max(0, depth - 1)
            child_text = []
            for child in node.get("content", []):
                self._walk_tiptap(child, child_text, depth)
            parts.append(f"{indent}- " + "".join(child_text))
            return

        if node_type == "paragraph":
            child_text = []
            for child in node.get("content", []):
                self._walk_tiptap(child, child_text, depth)
            parts.append("".join(child_text))
            return

        # Recurse into children
        new_depth = depth + 1 if node_type in ("bulletList", "orderedList") else depth
        for child in node.get("content", []):
            self._walk_tiptap(child, parts, new_depth)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _format_meetings_table(meetings: list[dict]) -> str:
    """Format meetings as a readable table."""
    if not meetings:
        return "No meetings found."

    lines = []
    for m in meetings:
        flags = []
        if m["has_panel"]:
            flags.append("notes")
        if m["has_transcript"]:
            flags.append("transcript")
        flags_str = f" [{', '.join(flags)}]" if flags else ""

        attendee_names = [a["name"] or a["email"] for a in m["attendees"]]
        attendees_str = ", ".join(attendee_names[:4])
        if len(attendee_names) > 4:
            attendees_str += f" +{len(attendee_names) - 4}"

        lines.append(f"{m['date']}  {m['id'][:8]}  {m['title']}")
        if attendees_str:
            lines.append(f"           {attendees_str}{flags_str}")
        else:
            lines.append(f"           (no attendees){flags_str}")
        lines.append("")

    return "\n".join(lines)


def _format_notes(notes: dict) -> str:
    """Format notes as readable markdown output."""
    lines = [f"# {notes['title']}", f"Date: {notes['date']}", ""]

    if notes["user_notes"]:
        lines.append("## Your notes")
        lines.append(notes["user_notes"])
        lines.append("")

    for panel in notes["panels"]:
        lines.append(f"## {panel['title']}")
        lines.append(panel["content"])
        lines.append("")

    return "\n".join(lines)


def _format_transcript(transcript: dict) -> str:
    """Format transcript as readable output."""
    lines = [
        f"# {transcript['title']}",
        f"Date: {transcript['date']}",
        f"Entries: {transcript['entry_count']}",
        "",
    ]

    if not transcript["entries"]:
        lines.append("(no transcript available)")
        return "\n".join(lines)

    for entry in transcript["entries"]:
        ts = entry["timestamp"][:19].replace("T", " ") if entry["timestamp"] else ""
        source = entry["source"]
        label = f"[{source}]"
        if entry.get("speaker"):
            label = f"[{entry['speaker']}]"
        lines.append(f"{ts}  {label} {entry['text']}")

    return "\n".join(lines)


def _format_search(results: list[dict]) -> str:
    """Format search results."""
    if not results:
        return "No results found."

    lines = []
    for r in results:
        lines.append(f"{r['date']}  {r['id'][:8]}  {r['title']}")
        for match in r["matches"]:
            lines.append(f"  > {match}")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Read Granola meeting notes from local cache"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # meetings
    p_meetings = sub.add_parser("meetings", help="List recent meetings")
    p_meetings.add_argument("--days", type=int, default=7, help="Lookback days (default: 7)")
    p_meetings.add_argument("--since", help="Start date (YYYY-MM-DD)")
    p_meetings.add_argument("--until", help="End date (YYYY-MM-DD)")
    p_meetings.add_argument("--limit", type=int, help="Max results")
    p_meetings.add_argument("--json", action="store_true", help="Output as JSON")

    # notes
    p_notes = sub.add_parser("notes", help="Get notes for a meeting")
    p_notes.add_argument("doc_id", help="Document ID (or first 8 chars)")
    p_notes.add_argument("--format", choices=["markdown", "plain", "html"], default="markdown")
    p_notes.add_argument("--json", action="store_true", help="Output as JSON")

    # transcript
    p_trans = sub.add_parser("transcript", help="Get transcript for a meeting")
    p_trans.add_argument("doc_id", help="Document ID (or first 8 chars)")
    p_trans.add_argument("--json", action="store_true", help="Output as JSON")

    # search
    p_search = sub.add_parser("search", help="Search meetings by keyword")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--days", type=int, help="Lookback days")
    p_search.add_argument("--transcripts", action="store_true", help="Also search transcripts")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--json", action="store_true", help="Output as JSON")

    # full
    p_full = sub.add_parser("full", help="Get everything for a meeting")
    p_full.add_argument("doc_id", help="Document ID (or first 8 chars)")
    p_full.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()
    gr = GranolaReader()

    # Resolve short doc IDs
    def resolve_doc_id(short_id: str) -> str:
        state = gr._load()
        docs = state["documents"]
        if short_id in docs:
            return short_id
        matches = [k for k in docs if k.startswith(short_id)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print(f"Ambiguous ID '{short_id}', matches: {matches}", file=sys.stderr)
            sys.exit(1)
        print(f"No document found matching '{short_id}'", file=sys.stderr)
        sys.exit(1)

    if args.command == "meetings":
        meetings = gr.get_meetings(
            days=args.days, since=args.since, until=args.until, limit=args.limit
        )
        if args.json:
            print(json.dumps(meetings, indent=2))
        else:
            print(_format_meetings_table(meetings))

    elif args.command == "notes":
        doc_id = resolve_doc_id(args.doc_id)
        notes = gr.get_notes(doc_id, format=args.format)
        if args.json:
            print(json.dumps(notes, indent=2))
        else:
            print(_format_notes(notes))

    elif args.command == "transcript":
        doc_id = resolve_doc_id(args.doc_id)
        transcript = gr.get_transcript(doc_id)
        if args.json:
            print(json.dumps(transcript, indent=2))
        else:
            print(_format_transcript(transcript))

    elif args.command == "search":
        results = gr.search(
            args.query,
            days=args.days,
            include_transcripts=args.transcripts,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            print(_format_search(results))

    elif args.command == "full":
        doc_id = resolve_doc_id(args.doc_id)
        full = gr.get_meeting_full(doc_id)
        if args.json:
            print(json.dumps(full, indent=2))
        else:
            # Print metadata
            print(f"# {full['title']}")
            print(f"Date: {full['date']}")
            if full["start_time"]:
                print(f"Time: {full['start_time']}")
            attendees = [a["name"] or a["email"] for a in full["attendees"]]
            if attendees:
                print(f"Attendees: {', '.join(attendees)}")
            print()

            if full["user_notes"]:
                print("## Your notes")
                print(full["user_notes"])
                print()

            for panel in full["panels"]:
                print(f"## {panel['title']}")
                print(panel["content"])
                print()

            if full["transcript"]:
                print(f"## Transcript ({full['transcript_entries']} entries)")
                for entry in full["transcript"]:
                    ts = entry["timestamp"][:19].replace("T", " ") if entry["timestamp"] else ""
                    source = entry.get("speaker") or entry["source"]
                    print(f"  {ts}  [{source}] {entry['text']}")


if __name__ == "__main__":
    main()
