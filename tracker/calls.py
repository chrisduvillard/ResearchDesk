"""Append-only revisions; approval time gates research availability."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlsplit
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from fastapi import HTTPException
from . import db
from .auth import audit
from .research import activity


class CallDocument(BaseModel):
    contributor_id: str = Field(max_length=80)
    asset_id: str = Field(max_length=160)
    action: Literal["long", "short", "close", "ambiguous"]
    spoken_at: datetime
    timezone: str = Field(max_length=80)
    source_url: str = Field(max_length=2048)
    excerpt: str = Field(min_length=1, max_length=2000)
    horizon: str = Field(min_length=1, max_length=300)
    target: float | None = Field(None, gt=0, allow_inf_nan=False)
    conditions: str = Field("", max_length=1000)
    instrument_type: Literal["underlying", "option"] = "underlying"
    revision: int | None = Field(None, ge=0)

    @field_validator("source_url")
    @classmethod
    def valid_link(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Use a public http(s) source link without credentials")
        return value

    @model_validator(mode="after")
    def validate_time(self):
        if self.spoken_at.utcoffset() is None:
            raise ValueError("Spoken timestamp must include an offset")
        try:
            zone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown timezone") from exc
        local = self.spoken_at.astimezone(zone)
        if self.spoken_at.utcoffset() != local.utcoffset():
            raise ValueError(
                "Timestamp offset does not match the timezone at that instant"
            )
        if self.spoken_at > db.utcnow():
            raise ValueError("Spoken timestamp cannot be in the future")
        return self


class Transition(BaseModel):
    revision: int = Field(ge=1)
    reason: str = Field("", max_length=2000)


def get_call(conn, call_id):
    row = conn.execute(
        """SELECT c.id,c.contributor_id,r.* FROM calls c JOIN call_revisions r
       ON r.id=c.current_revision WHERE c.id=?""",
        (call_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Call not found")
    result = dict(row)
    result["id"] = call_id
    result["document"] = json.loads(result.pop("document_json"))
    return result


def revise(conn, payload=None, call_id=None, transition=None, status="draft"):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        now = db.iso()
        if call_id is None:
            if not conn.execute(
                "SELECT 1 FROM contributors WHERE id=?", (payload.contributor_id,)
            ).fetchone():
                raise HTTPException(422, "Unknown contributor")
            call_id = conn.execute(
                "INSERT INTO calls(contributor_id,created_at) VALUES(?,?)",
                (payload.contributor_id, now),
            ).lastrowid
            revision, doc = 1, payload.model_dump(mode="json", exclude={"revision"})
        else:
            old = get_call(conn, call_id)
            expected = payload.revision if payload else transition.revision
            if expected != old["revision"]:
                raise HTTPException(409, "Call changed; reload the latest revision")
            if status != "draft" and old["status"] == status:
                raise HTTPException(409, "Call already has that state")
            if status == "approved" and old["status"] != "draft":
                raise HTTPException(409, "Only drafts can be approved")
            if status == "retracted" and not transition.reason.strip():
                raise HTTPException(422, "Retraction reason required")
            doc = (
                payload.model_dump(mode="json", exclude={"revision"})
                if payload
                else old["document"]
            )
            if doc["contributor_id"] != old["contributor_id"]:
                raise HTTPException(
                    422, "Create a separate call to change its contributor"
                )
            revision = old["revision"] + 1
            # Old saved analysis keeps its frozen input; future runs exclude superseded evidence.
            conn.execute(
                "UPDATE research_events SET invalidated_at=coalesce(invalidated_at,?),eligible=0 WHERE call_revision_id IN (SELECT id FROM call_revisions WHERE call_id=?)",
                (now, call_id),
            )
        asset = conn.execute(
            "SELECT * FROM assets WHERE id=?", (doc["asset_id"],)
        ).fetchone()
        if not asset:
            raise HTTPException(422, "Unknown instrument; choose an asset from search")
        revision_id = conn.execute(
            "INSERT INTO call_revisions(call_id,revision,status,document_json,recorded_at,approved_at) VALUES(?,?,?,?,?,?)",
            (
                call_id,
                revision,
                status,
                json.dumps(doc),
                now,
                now if status == "approved" else None,
            ),
        ).lastrowid
        conn.execute(
            "UPDATE calls SET current_revision=? WHERE id=?", (revision_id, call_id)
        )
        audit(
            conn,
            status,
            "call",
            call_id,
            {"revision": revision, "reason": transition.reason if transition else ""},
        )
        source = "cnbc:" + doc["contributor_id"]
        if status == "approved":
            available = max(db.iso(datetime.fromisoformat(doc["spoken_at"])), now)
            eligible = bool(
                asset["verified"]
                and doc["action"] in ("long", "short")
                and not doc["conditions"]
                and doc["instrument_type"] == "underlying"
            )
            conn.execute(
                """INSERT INTO research_events(contributor_id,evidence_type,asset_id,symbol,kind,direction,eligible,
              public_at,available_at,source_url,excerpt,call_revision_id,details_json) VALUES(?,'call',?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    doc["contributor_id"],
                    asset["id"],
                    asset["symbol"],
                    "close" if doc["action"] == "close" else "call",
                    {"long": "bullish", "short": "bearish"}.get(
                        doc["action"], "unknown"
                    ),
                    int(eligible),
                    db.iso(datetime.fromisoformat(doc["spoken_at"])),
                    available,
                    doc["source_url"],
                    doc["excerpt"],
                    revision_id,
                    json.dumps(doc | {"exit_eligible": bool(asset["verified"])}),
                ),
            )
            activity(
                conn,
                f"call:{revision_id}",
                source,
                asset["id"],
                "call",
                doc["spoken_at"],
                available,
                f"{doc['contributor_id']}: {asset['symbol']} call approved",
                doc["source_url"],
                "call",
                call_id,
            )
        elif revision > 1:
            activity(
                conn,
                f"call:{revision_id}",
                source,
                asset["id"],
                "correction",
                doc["spoken_at"],
                now,
                f"Call {call_id}: {status}",
                doc["source_url"],
                "call",
                call_id,
            )
        return get_call(conn, call_id)
