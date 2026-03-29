import os
import base64
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from supabase import create_client, Client
import uvicorn

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)

port = int(os.environ.get("PORT", 8000))
bucket = os.environ.get("STORAGE_BUCKET", "zettel-attachments")
mcp = FastMCP("zettelkasten", host="0.0.0.0", port=port)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_tags() -> list[str]:
    """
    Return all tags currently in use across the zettelkasten, sorted alphabetically.
    Call this before drafting a zettel to check existing tags and reuse them where
    appropriate rather than creating near-duplicates.
    """
    result = supabase.rpc("get_all_tags").execute()
    return result.data or []


@mcp.tool()
def draft_zettel(
    title: str,
    body: str,
    author: str = "unknown",
    type: str = "note",
    tags: list[str] = [],
    fields: dict = {},
) -> dict:
    """
    Structure a zettel entry and return it for user review WITHOUT saving to the database.

    Trigger: when the user says "ST" or "ST <context>", treat it as an instruction
    to save the most recent idea, concept, or content from the conversation to the zettelkasten.

    Workflow:
    1. Ask the user for their name if not already known, to populate the author field.
    2. Call get_tags() first to see existing tags.
    3. Call this tool to produce the draft.
    4. Present the draft clearly to the user and invite edits.
    5. If the user requests changes, call draft_zettel again with the updated values.
    6. Only call commit_zettel once the user explicitly approves.

    Tag rules — enforce strictly:
    - Always lowercase
    - Hyphens instead of spaces (e.g. "machine-learning" not "machine learning")
    - Prefer existing tags over creating new ones where the meaning is the same
    - Max ~5 tags per entry

    Types: note | idea | contact | organization | reference | todo

    Per-type field schemas for the `fields` parameter:

      note:         {}  (no extra fields needed)

      idea:         {"status": "raw|developing|mature", "source": "..."}

      todo:         {"status": "pending|in-progress|done",
                     "priority": "high|medium|low",
                     "due_date": "YYYY-MM-DD",
                     "owner": "..."}

      contact:      {"first_name": "...", "last_name": "...", "email": "...",
                     "phone": "...", "organization": "...", "role": "...",
                     "website": "..."}

      organization: {"website": "...", "industry": "...", "email": "...",
                     "phone": "...", "address": "..."}

      reference:    {"url": "...", "source": "...", "ref_author": "...",
                     "published_date": "YYYY-MM-DD"}
    """
    return {
        "status": "draft — not yet saved",
        "title": title,
        "body": body,
        "type": type,
        "tags": tags,
        "fields": fields,
        "author": author,
    }


@mcp.tool()
def commit_zettel(
    title: str,
    body: str,
    author: str = "unknown",
    type: str = "note",
    tags: list[str] = [],
    fields: dict = {},
) -> dict:
    """
    Save a zettel entry to the database.

    IMPORTANT: Always call draft_zettel first and get explicit user approval
    before calling this tool. Never commit without the user confirming the draft.

    Types: note | idea | contact | organization | reference | todo

    See draft_zettel for the per-type field schemas for the `fields` parameter.
    """
    result = (
        supabase.table("zettels")
        .insert({"title": title, "body": body, "type": type, "tags": tags, "fields": fields, "author": author})
        .execute()
    )
    entry = result.data[0]
    return {"status": "saved", "id": entry["id"], "title": entry["title"], "author": entry["author"]}


@mcp.tool()
def update_zettel(
    id: str,
    title: str | None = None,
    body: str | None = None,
    type: str | None = None,
    tags: list[str] | None = None,
    fields: dict | None = None,
) -> dict:
    """
    Update one or more fields on an existing zettel. Only supplied fields are changed.
    To merge fields rather than replace them, fetch the entry first with get_zettel.
    See draft_zettel for per-type field schemas.
    """
    updates: dict = {}
    if title  is not None: updates["title"]  = title
    if body   is not None: updates["body"]   = body
    if type   is not None: updates["type"]   = type
    if tags   is not None: updates["tags"]   = tags
    if fields is not None: updates["fields"] = fields

    if not updates:
        return {"status": "no_changes"}

    result = (
        supabase.table("zettels")
        .update(updates)
        .eq("id", id)
        .execute()
    )
    if not result.data:
        return {"status": "not_found"}
    return {"status": "updated", "id": id}


@mcp.tool()
def delete_zettel(id: str) -> dict:
    """
    Permanently delete a zettel and all its links from the database.

    IMPORTANT: This is irreversible. Always confirm with the user before calling this tool.
    Show the zettel title (via get_zettel) and ask for explicit approval first.
    """
    # Remove all links where this zettel is source or target
    supabase.table("zettel_links").delete().eq("source_id", id).execute()
    supabase.table("zettel_links").delete().eq("target_id", id).execute()

    result = supabase.table("zettels").delete().eq("id", id).execute()
    if not result.data:
        return {"status": "not_found"}
    return {"status": "deleted", "id": id, "title": result.data[0]["title"]}


@mcp.tool()
def get_zettel(id: str) -> dict:
    """Fetch a single zettel by its UUID, including any linked zettels."""
    result = (
        supabase.table("zettels")
        .select("*")
        .eq("id", id)
        .execute()
    )
    if not result.data:
        return {"error": "not found"}

    entry = result.data[0]

    # Fetch outbound links
    links_result = (
        supabase.table("zettel_links")
        .select("target_id, relationship, zettels!zettel_links_target_id_fkey(id, title, type)")
        .eq("source_id", id)
        .execute()
    )
    entry["links"] = links_result.data or []
    return entry


@mcp.tool()
def search_zettels(
    query: str | None = None,
    tags: list[str] | None = None,
    type: str | None = None,
    limit: int = 20,
) -> list:
    """
    Search zettels. Supports full-text search (query), tag filtering, and type filtering.
    Returns id, title, type, tags, and a body snippet.
    """
    q = supabase.table("zettels").select("id, title, type, tags, body, author, created_at")

    if query:
        q = q.text_search("fts", query)
    if tags:
        q = q.contains("tags", tags)
    if type:
        q = q.eq("type", type)

    result = q.order("created_at", desc=True).limit(limit).execute()

    # Trim body to a short snippet for readability
    entries = []
    for row in result.data:
        row["snippet"] = (row.get("body") or "")[:200]
        del row["body"]
        entries.append(row)
    return entries


@mcp.tool()
def list_recent(limit: int = 10) -> list:
    """List the most recently created zettel entries."""
    result = (
        supabase.table("zettels")
        .select("id, title, type, tags, author, created_at")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data


@mcp.tool()
def batch_commit(entries: list[dict]) -> dict:
    """
    Save multiple zettel entries to the database in one call.

    Each entry in the list should have the same fields as commit_zettel:
      title (required), body, type, tags, fields

    Use this after drafting multiple entries and getting explicit user approval
    for all of them. Present all drafts clearly before calling this tool.

    Example entries:
      [
        {"title": "Idea A", "body": "...", "type": "idea", "tags": ["ai"]},
        {"title": "Task X", "body": "...", "type": "todo", "fields": {"status": "pending", "priority": "high"}}
      ]
    """
    rows = [
        {
            "title": e["title"],
            "body": e.get("body", ""),
            "type": e.get("type", "note"),
            "tags": e.get("tags", []),
            "fields": e.get("fields", {}),
            "author": e.get("author", "unknown"),
        }
        for e in entries
    ]
    result = supabase.table("zettels").insert(rows).execute()
    return {"status": "saved", "count": len(result.data), "ids": [r["id"] for r in result.data]}


@mcp.tool()
def list_todos(status: str | None = None) -> list:
    """
    Return todo entries, optionally filtered by status.
    status: "pending" | "in-progress" | "done" | None (returns all)
    Results are ordered by priority (high → medium → low) then created_at.
    """
    result = (
        supabase.table("zettels")
        .select("id, title, tags, fields, created_at")
        .eq("type", "todo")
        .execute()
    )
    todos = result.data or []

    if status:
        todos = [t for t in todos if t.get("fields", {}).get("status") == status]

    priority_order = {"high": 0, "medium": 1, "low": 2}
    todos.sort(key=lambda t: (
        priority_order.get(t.get("fields", {}).get("priority", "low"), 2),
        t["created_at"]
    ))
    return todos


@mcp.tool()
def link_zettels(
    source_id: str,
    target_id: str,
    relationship: str = "related",
) -> dict:
    """
    Create a directional link between two zettels.
    Relationships: related | inspired_by | part_of | contradicts | see_also
    """
    result = (
        supabase.table("zettel_links")
        .insert({"source_id": source_id, "target_id": target_id, "relationship": relationship})
        .execute()
    )
    return {"status": "linked", "relationship": relationship}


# ---------------------------------------------------------------------------
# Attachment tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_attachments(zettel_id: str) -> list:
    """List all attachments for a given zettel."""
    result = (
        supabase.table("zettel_attachments")
        .select("id, filename, mime_type, size_bytes, uploaded_by, created_at")
        .eq("zettel_id", zettel_id)
        .order("created_at", desc=False)
        .execute()
    )
    return result.data or []


@mcp.tool()
def upload_attachment(
    zettel_id: str,
    filename: str,
    content_base64: str,
    mime_type: str = "application/octet-stream",
    uploaded_by: str = "unknown",
) -> dict:
    """
    Upload a file and attach it to a zettel.

    content_base64: the file contents encoded as a base64 string.
    Suitable for documents, PDFs, images, and other files shared in the conversation.
    The storage path will be: {zettel_id}/{filename}

    IMPORTANT: Confirm with the user which zettel to attach the file to before calling this.
    """
    data = base64.b64decode(content_base64)
    storage_path = f"{zettel_id}/{filename}"

    supabase.storage.from_(bucket).upload(
        path=storage_path,
        file=data,
        file_options={"content-type": mime_type, "upsert": "true"},
    )

    result = (
        supabase.table("zettel_attachments")
        .insert({
            "zettel_id": zettel_id,
            "filename": filename,
            "storage_path": storage_path,
            "mime_type": mime_type,
            "size_bytes": len(data),
            "uploaded_by": uploaded_by,
        })
        .execute()
    )
    entry = result.data[0]
    return {"status": "uploaded", "id": entry["id"], "filename": filename, "size_bytes": len(data)}


@mcp.tool()
def get_attachment_url(attachment_id: str, expires_in: int = 3600) -> dict:
    """
    Generate a signed download URL for an attachment.
    expires_in: seconds until the URL expires (default 1 hour, max 604800 / 7 days).
    """
    result = (
        supabase.table("zettel_attachments")
        .select("storage_path, filename")
        .eq("id", attachment_id)
        .execute()
    )
    if not result.data:
        return {"error": "not found"}

    storage_path = result.data[0]["storage_path"]
    signed = supabase.storage.from_(bucket).create_signed_url(storage_path, expires_in)
    return {"url": signed["signedURL"], "filename": result.data[0]["filename"], "expires_in": expires_in}


@mcp.tool()
def delete_attachment(attachment_id: str) -> dict:
    """
    Permanently delete an attachment from storage and the database.
    IMPORTANT: This is irreversible. Confirm with the user before calling.
    """
    result = (
        supabase.table("zettel_attachments")
        .select("storage_path, filename")
        .eq("id", attachment_id)
        .execute()
    )
    if not result.data:
        return {"status": "not_found"}

    storage_path = result.data[0]["storage_path"]
    supabase.storage.from_(bucket).remove([storage_path])
    supabase.table("zettel_attachments").delete().eq("id", attachment_id).execute()
    return {"status": "deleted", "filename": result.data[0]["filename"]}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="sse")
