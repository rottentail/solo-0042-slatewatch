"""Probe: same surface state from two event histories, plus a late intent reversal.

Runs the real Slatewatch service in-process against a real ``basalt`` MKV produced
by ``slatewatch.reels``. The media bytes are generated once and ingested into two
separate vaults, so both share master/scan/hit identifiers while each keeps an
independent review-event sequence (SQLite rowids start over at seq=1 per vault).
Everything here reads only public service methods and the on-disk ZIP that a
receiving party would actually open.
"""
import json
import sys
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from slatewatch.reels import make
from slatewatch.review import adjudicate
from slatewatch.rules import canonical
from slatewatch.service import Slatewatch


def new_workbench(root: Path, label: str, media_bytes: bytes):
    """Build a vault over fixed media bytes, register master + policy, scan.

    The master label is fixed: master identifiers hash the label too, so the two
    vaults only share master/scan/hit ids when their intake descriptors match.
    """
    app = Slatewatch(root / f"vault-{label}")
    master = app.master(media_bytes, "basalt-shared", None, "first")
    policy = app.policy({"label": "bench default"})
    scan = app.scan({"master_id": master["id"], "policy_id": policy["id"]})
    return app, scan


def hit_of(scan, kind, channel=None):
    for item in scan["hits"]:
        if item["kind"] == kind and item.get("channel") == channel:
            return item
    raise KeyError((kind, channel))


def ruling(scan_id, hit, outcome, reason, reviewer):
    return {"hit_id": hit["id"], "outcome": outcome, "reason": reason,
            "reviewer": reviewer}


def surface(review):
    """What a verdict-only snapshot would carry: per-hit status and totals."""
    return {
        "status": review["status"],
        "counts": review["counts"],
        "hits": sorted((h["id"], h["status"]) for h in review["hits"]),
    }


def prefix_fingerprint(review):
    """What a full event prefix carries: ordered intents + full rulings."""
    return sha256(canonical({"intents": review["intents"],
                             "rulings": review["rulings"]})).hexdigest()[:16]


def snapshot_id(scan, review):
    """Identifier derivable when only the projected snapshot is kept."""
    return "snapshot-" + sha256(
        canonical({"scan_id": scan["id"], "surface": surface(review)})
    ).hexdigest()[:16]


def main():
    with TemporaryDirectory(prefix="sw-media-") as media_tmp:
        media_bytes = make(Path(media_tmp) / "basalt.mkv", "basalt").read_bytes()

        print("=" * 78)
        print("PAIR A — two histories, identical surface: ruling churn vs one hold")
        print("=" * 78)
        with TemporaryDirectory(prefix="sw-pair-a-") as tmp:
            root = Path(tmp)
            app_a, scan_a = new_workbench(root, "a", media_bytes)
            app_b, scan_b = new_workbench(root, "b", media_bytes)
            assert scan_a["id"] == scan_b["id"], "same bytes + policy must share scan id"
            clip = hit_of(scan_a, "clip", 2)

            # History A: accept -> reset -> hold (three rulings, last one wins).
            app_a.add_ruling(scan_a["id"], ruling(scan_a["id"], clip, "accept",
                                                  "first pass: accept loud passage", "reviewer-1"))
            app_a.add_ruling(scan_a["id"], ruling(scan_a["id"], clip, "reset",
                                                  "second look requested", "reviewer-1"))
            app_a.add_ruling(scan_a["id"], ruling(scan_a["id"], clip, "hold",
                                                  "confirmed full-scale clipping", "reviewer-2"))
            review_a = app_a.review(scan_a["id"])

            # History B: a single hold ruling.
            app_b.add_ruling(scan_b["id"], ruling(scan_b["id"], clip, "hold",
                                                  "confirmed full-scale clipping", "reviewer-2"))
            review_b = app_b.review(scan_b["id"])

            print("A surface:", json.dumps(surface(review_a)["counts"], ensure_ascii=False),
                  "rollup =", review_a["status"])
            print("B surface:", json.dumps(surface(review_b)["counts"], ensure_ascii=False),
                  "rollup =", review_b["status"])
            print("surface equal:        ", surface(review_a) == surface(review_b))
            print("snapshot id equal:    ",
                  snapshot_id(scan_a, review_a) == snapshot_id(scan_b, review_b))
            print("prefix fingerprint A: ", prefix_fingerprint(review_a),
                  "ruling seqs =", [r["seq"] for r in review_a["rulings"]])
            print("  A prefix retains superseded outcomes:",
                  [(r["seq"], r["outcome"]) for r in review_a["rulings"]])
            clip_row_a = next(h for h in review_a["hits"] if h["id"] == clip["id"])
            print("  A projected ruling keeps only the last one:",
                  clip_row_a["ruling"]["outcome"])
            print("prefix fingerprint B: ", prefix_fingerprint(review_b),
                  "ruling seqs =", [r["seq"] for r in review_b["rulings"]])
            print("prefixes equal:       ",
                  prefix_fingerprint(review_a) == prefix_fingerprint(review_b))

            # Seal both with the same excerpt options.
            options = {"hit_ids": [clip["id"]], "padding_ms": 80, "frames": 3}
            seal_a = app_a.seal(scan_a["id"], options)
            seal_b = app_b.seal(scan_b["id"], options)
            print("seal ids equal:       ", seal_a["id"] == seal_b["id"])
            print("  seal A:", seal_a["id"], "status", seal_a["review_status"])
            print("  seal B:", seal_b["id"], "status", seal_b["review_status"])

        print()
        print("=" * 78)
        print("PAIR B — same surface, one intent vs two-event partial-then-complete")
        print("=" * 78)
        with TemporaryDirectory(prefix="sw-pair-b-") as tmp:
            root = Path(tmp)
            app_c, scan_c = new_workbench(root, "c", media_bytes)
            app_d, scan_d = new_workbench(root, "d", media_bytes)
            silence = hit_of(scan_c, "silence", 1)
            span = {"start_us": silence["start_us"], "end_us": silence["end_us"]}

            # History C: one production intent fully covering the silence.
            app_c.add_intent(scan_c["id"], {"kind": "silence", "channel": 1, **span,
                                            "reason": "scheduled left-channel quiet bed"})
            review_c = app_c.review(scan_c["id"])

            # History D: a partial intent first (candidate stays pending), then a
            # completing intent — two events projecting onto the same surface.
            app_d.add_intent(scan_d["id"], {"kind": "silence", "channel": 1,
                                            "start_us": span["start_us"],
                                            "end_us": span["end_us"] - 1,
                                            "reason": "first partial annotation"})
            partial_d = app_d.review(scan_d["id"])
            app_d.add_intent(scan_d["id"], {"kind": "silence", "channel": 1, **span,
                                            "reason": "completed cover annotation"})
            review_d = app_d.review(scan_d["id"])

            status_c = next(h for h in review_c["hits"] if h["id"] == silence["id"])["status"]
            status_d_partial = next(h for h in partial_d["hits"]
                                    if h["id"] == silence["id"])["status"]
            status_d = next(h for h in review_d["hits"] if h["id"] == silence["id"])["status"]
            print("C silence status:", status_c,
                  "| D after partial intent:", status_d_partial,
                  "| D after second intent:", status_d)
            print("surface equal:     ", surface(review_c) == surface(review_d))
            print("snapshot id equal: ",
                  snapshot_id(scan_c, review_c) == snapshot_id(scan_d, review_d))
            print("C intent seqs:", [i["seq"] for i in review_c["intents"]],
                  "D intent seqs:", [i["seq"] for i in review_d["intents"]])
            print("prefixes equal:    ",
                  prefix_fingerprint(review_c) == prefix_fingerprint(review_d))
            print("offline-visible reasons — C:",
                  review_c["intents"][0]["reason"],
                  "| D:", [i["reason"] for i in review_d["intents"]])

        print()
        print("=" * 78)
        print("LATE INTENT — intent that arrives after the human ruling")
        print("=" * 78)
        with TemporaryDirectory(prefix="sw-late-") as tmp:
            root = Path(tmp)
            app_e, scan_e = new_workbench(root, "e", media_bytes)
            black = hit_of(scan_e, "black")
            clip = hit_of(scan_e, "clip", 2)
            sid = scan_e["id"]

            # 1) reviewer holds both findings before any intent exists.
            app_e.add_ruling(sid, ruling(sid, black, "hold",
                                         "unexplained opening black", "reviewer-1"))
            app_e.add_ruling(sid, ruling(sid, clip, "hold",
                                         "full-scale right channel", "reviewer-1"))
            sealed_before = app_e.seal(sid, {"hit_ids": [black["id"], clip["id"]],
                                             "padding_ms": 80, "frames": 3})
            review_before = app_e.review(sid)
            print("before late intent: rollup =", review_before["status"],
                  "counts =", json.dumps(review_before["counts"]))
            print("sealed id:", sealed_before["id"])

            # 2) production intent for the black episode arrives late.
            late = app_e.add_intent(sid, {"kind": "black",
                                          "start_us": black["start_us"],
                                          "end_us": black["end_us"],
                                          "reason": "slate black was always planned"})
            review_after = app_e.review(sid)
            black_row = next(h for h in review_after["hits"] if h["id"] == black["id"])
            clip_row = next(h for h in review_after["hits"] if h["id"] == clip["id"])
            print("late intent seq:", late["seq"])
            print("black hit now:", black_row["status"],
                  "| intent_sequences:", black_row["intent_sequences"],
                  "| ruling still attached:", black_row["ruling"]["outcome"])
            print("clip hit now: ", clip_row["status"], "(manual ruling outranks intent)")
            print("rollup after late intent:", review_after["status"],
                  "counts =", json.dumps(review_after["counts"]))

            # 3) reviewer resets the black hold: the late intent now decides it.
            app_e.add_ruling(sid, ruling(sid, black, "reset",
                                         "withdraw hold, slate intent accepted", "reviewer-1"))
            review_reset = app_e.review(sid)
            black_reset = next(h for h in review_reset["hits"] if h["id"] == black["id"])
            print("after reset: black hit =", black_reset["status"],
                  "intent_sequences:", black_reset["intent_sequences"],
                  "| rollup =", review_reset["status"],
                  "counts =", json.dumps(review_reset["counts"]))

            # 4) re-seal and compare against the earlier sealed bundle.
            sealed_after = app_e.seal(sid, {"hit_ids": [black["id"], clip["id"]],
                                            "padding_ms": 80, "frames": 3})
            print("re-sealed id differs:", sealed_after["id"] != sealed_before["id"])
            print("  old:", sealed_before["id"],
                  "review_status =", sealed_before["review_status"])
            print("  new:", sealed_after["id"],
                  "review_status =", sealed_after["review_status"])

            # 5) what the offline receiver sees inside the OLD bundle.
            verified = app_e.verify(sealed_before["id"])
            bundle_bytes = app_e.download(sealed_before["id"])
            with ZipFile(BytesIO(bundle_bytes)) as zf:
                old_review = json.loads(zf.read("review.json"))
                names = sorted(zf.namelist())
            old_black = next(h for h in old_review["hits"] if h["id"] == black["id"])
            print("old bundle still verifies:", verified["status"],
                  "| members:", verified["members"])
            print("old bundle review.json intents:", len(old_review["intents"]),
                  "| black status frozen at:", old_black["status"],
                  "| black intent_sequences:", old_black["intent_sequences"])
            print("old bundle members:", names)

            # 6) replay check: re-adjudicate the prefix embedded in the old bundle
            #    against the same immutable scan.
            replayed = adjudicate(app_e.vault.get("scan", sid),
                                  old_review["intents"], old_review["rulings"])
            print("replay of embedded prefix reproduces frozen surface:",
                  surface(replayed) == surface(old_review))

            # 7) what the reset-time judgement depends on: the intent prefix.
            #    Replay the final state with the intent removed, as a
            #    verdict-only snapshot would force an offline checker to do.
            rulings_only = adjudicate(app_e.vault.get("scan", sid),
                                      [], review_reset["rulings"])
            black_no_intent = next(h for h in rulings_only["hits"] if h["id"] == black["id"])
            black_with_prefix = next(h for h in review_reset["hits"] if h["id"] == black["id"])
            print("reset replay with full prefix:    black =", black_with_prefix["status"])
            print("reset replay without intent prefix: black =", black_no_intent["status"],
                  "<- snapshot-only material cannot explain the expected verdict")

            # A snapshot-only view collapses the distinction.
            snap_before = snapshot_id(scan_e, review_before)
            snap_after = snapshot_id(scan_e, review_after)
            snap_reset = snapshot_id(scan_e, review_reset)
            print("snapshot-only ids — before:", snap_before, "after:", snap_after,
                  "equal:", snap_before == snap_after)
            print("snapshot id after reset differs from sealed one:",
                  snap_reset != snap_before)


if __name__ == "__main__":
    main()
