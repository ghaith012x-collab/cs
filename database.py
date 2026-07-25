"""
Knowledge Database — PostgreSQL-backed auto-learning system.

Every captcha the bot encounters (grid tiles, drag challenges, sliders)
gets saved here. Over time this builds a labeled dataset we can train on.

Schema:
  - tiles:          Grid challenge tile images + labels
  - drag_challenges: Drag iframe screenshots + object/target positions
  - slider_records:  Slider screenshots + offset values
  - knowledge:       Quick lookup: class_name → how to detect it

Environment:
  DATABASE_URL=postgres://user:pass@host:5432/dbname
  If not set, the DB runs in no-op mode (safely does nothing).
"""

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Optional

import asyncpg


# ── Table Creation SQL ────────────────────────────────────

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS tiles (
    id          BIGSERIAL PRIMARY KEY,
    class_name  TEXT NOT NULL,
    image_b64   TEXT NOT NULL,
    challenge   TEXT,
    confidence  REAL DEFAULT 0.0,
    success     BOOLEAN DEFAULT FALSE,
    captured_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tiles_class ON tiles(class_name);
CREATE INDEX IF NOT EXISTS idx_tiles_success ON tiles(success);

CREATE TABLE IF NOT EXISTS drag_challenges (
    id              BIGSERIAL PRIMARY KEY,
    class_name      TEXT NOT NULL,
    target_class    TEXT NOT NULL DEFAULT '',
    iframe_b64      TEXT,
    object_x        REAL,
    object_y        REAL,
    target_x        REAL,
    target_y        REAL,
    challenge_text  TEXT,
    success         BOOLEAN DEFAULT FALSE,
    solved_by       TEXT DEFAULT '',  -- 'js', 'yolo', 'opencv', 'sweep'
    captured_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_drag_class ON drag_challenges(class_name);

CREATE TABLE IF NOT EXISTS slider_records (
    id          BIGSERIAL PRIMARY KEY,
    screenshot_b64 TEXT,
    offset_px   INTEGER,
    challenge_text TEXT,
    success     BOOLEAN DEFAULT FALSE,
    captured_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS knowledge (
    id              BIGSERIAL PRIMARY KEY,
    class_name      TEXT UNIQUE NOT NULL,
    category        TEXT DEFAULT 'grid',  -- 'grid', 'drag', 'slider'
    sample_count    INTEGER DEFAULT 0,
    confidence      REAL DEFAULT 0.0,
    template_key    TEXT DEFAULT '',
    last_seen_at    TIMESTAMPTZ DEFAULT NOW(),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_class ON knowledge(class_name);
"""


# ── Database Connection ───────────────────────────────────

class KnowledgeDB:
    """Postgres-backed knowledge database for auto-learning captcha objects.
    
    Usage:
        db = await KnowledgeDB.create()
        await db.save_tile("star", image_b64, challenge="click all stars")
        records = await db.get_tiles("star", limit=50)
        await db.close()
    
    If DATABASE_URL is not set, all methods are no-ops — safe to use
    without a database.
    """

    def __init__(self, pool: Optional[asyncpg.Pool] = None,
                 log: Optional[Callable] = None):
        self.pool = pool
        self._log = log or (lambda msg, level="info": None)
        self._noop = pool is None

    @classmethod
    async def create(cls, dsn: Optional[str] = None,
                     log: Optional[Callable] = None) -> "KnowledgeDB":
        """Create a KnowledgeDB instance.
        
        Reads DATABASE_URL from env if dsn not provided.
        Returns a no-op instance if no URL is configured.
        """
        _log = log or (lambda msg, level="info": None)
        dsn = dsn or os.environ.get("DATABASE_URL")
        
        if not dsn:
            _log("[DB] DATABASE_URL not set — running in no-op mode", level="warn")
            return cls(pool=None, log=log)
        
        try:
            pool = await asyncpg.create_pool(
                dsn, min_size=1, max_size=4,
                command_timeout=10,
            )
            # Create tables
            async with pool.acquire() as conn:
                await conn.execute(CREATE_TABLES_SQL)
            _log("[DB] Connected to PostgreSQL and tables ready")
            return cls(pool=pool, log=log)
        except Exception as e:
            _log(f"[DB] Connection failed: {e} — running in no-op mode",
                 level="error")
            return cls(pool=None, log=log)

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None
            self._noop = True

    # ── Auto-label helpers ────────────────────────────────

    @staticmethod
    def _clean_class(text: str) -> str:
        """Extract a clean class name from challenge text.
        E.g. 'click all images containing a star' → 'star'
             'select all squares with buses' → 'bus'
             'drag the rocketship to the star' → 'rocketship'
        """
        t = text.lower().strip()
        # Remove common prefixes
        for p in ['please ', 'kindly ', 'now ']:
            if t.startswith(p):
                t = t[len(p):].strip()
        # Extract the object name after key phrases
        for phrase in ['select all images containing ',
                       'click all images with ',
                       'select all squares with ',
                       'click all squares containing ',
                       'choose all images with ',
                       'select all matching ',
                       'click all ',
                       'select all ',
                       'choose all ']:
            if phrase in t:
                subj = t.split(phrase, 1)[1].strip().strip('.!?,:;')
                for art in ['a ', 'an ', 'the ']:
                    if subj.startswith(art):
                        subj = subj[len(art):]
                return subj.split()[0] if subj else "unknown"
        for kw in ['containing ', 'with ', 'matching ', 'showing ']:
            if kw in t:
                subj = t.split(kw, 1)[1].strip().strip('.!?,:;')
                for art in ['a ', 'an ', 'the ']:
                    if subj.startswith(art):
                        subj = subj[len(art):]
                return subj.split()[0] if subj else "unknown"
        return "unknown"

    # ── Tiles (Grid Challenges) ──────────────────────────

    async def save_tile(self, class_name: str, image_b64: str,
                        challenge: str = "", confidence: float = 0.0,
                        success: bool = False) -> bool:
        """Save a grid tile image with its label."""
        if self._noop:
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO tiles (class_name, image_b64, challenge,
                                          confidence, success)
                       VALUES ($1, $2, $3, $4, $5)""",
                    class_name, image_b64, challenge, confidence, success)
                # Update knowledge table
                await conn.execute(
                    """INSERT INTO knowledge (class_name, category,
                                              sample_count, last_seen_at)
                       VALUES ($1, 'grid', 1, NOW())
                       ON CONFLICT (class_name)
                       DO UPDATE SET sample_count = knowledge.sample_count + 1,
                                     last_seen_at = NOW()""",
                    class_name)
            return True
        except Exception as e:
            self._log(f"[DB] save_tile error: {e}", level="error")
            return False

    async def save_tiles_batch(self, tiles: list[dict]) -> int:
        """Save multiple tiles at once.
        
        Each dict: {class_name, image_b64, challenge, confidence, success}
        """
        if self._noop or not tiles:
            return 0
        try:
            async with self.pool.acquire() as conn:
                count = 0
                for tile in tiles:
                    await conn.execute(
                        """INSERT INTO tiles (class_name, image_b64, challenge,
                                              confidence, success)
                           VALUES ($1, $2, $3, $4, $5)""",
                        tile.get("class_name", "unknown"),
                        tile.get("image_b64", ""),
                        tile.get("challenge", ""),
                        tile.get("confidence", 0.0),
                        tile.get("success", False))
                    count += 1
                # Bulk update knowledge counts
                classes = {}
                for t in tiles:
                    cn = t.get("class_name", "unknown")
                    classes[cn] = classes.get(cn, 0) + 1
                for cn, cnt in classes.items():
                    await conn.execute(
                        """INSERT INTO knowledge (class_name, category,
                                                  sample_count, last_seen_at)
                           VALUES ($1, 'grid', $2, NOW())
                           ON CONFLICT (class_name)
                           DO UPDATE SET sample_count = knowledge.sample_count + $2,
                                         last_seen_at = NOW()""",
                        cn, cnt)
                self._log(f"[DB] Saved {count} tiles batch")
                return count
        except Exception as e:
            self._log(f"[DB] save_tiles_batch error: {e}", level="error")
            return 0

    async def get_tiles(self, class_name: str, limit: int = 100,
                        only_success: bool = True) -> list[dict]:
        """Get labeled tiles for a class."""
        if self._noop:
            return []
        try:
            async with self.pool.acquire() as conn:
                if only_success:
                    rows = await conn.fetch(
                        """SELECT id, class_name, image_b64, challenge,
                                  confidence, success, captured_at
                           FROM tiles
                           WHERE class_name = $1 AND success = TRUE
                           ORDER BY captured_at DESC LIMIT $2""",
                        class_name, limit)
                else:
                    rows = await conn.fetch(
                        """SELECT id, class_name, image_b64, challenge,
                                  confidence, success, captured_at
                           FROM tiles
                           WHERE class_name = $1
                           ORDER BY captured_at DESC LIMIT $2""",
                        class_name, limit)
                return [dict(r) for r in rows]
        except Exception as e:
            self._log(f"[DB] get_tiles error: {e}", level="error")
            return []

    async def count_tiles(self, class_name: Optional[str] = None) -> dict:
        """Get tile counts, optionally filtered by class."""
        if self._noop:
            return {}
        try:
            async with self.pool.acquire() as conn:
                if class_name:
                    row = await conn.fetchrow(
                        "SELECT COUNT(*) as cnt FROM tiles WHERE class_name = $1",
                        class_name)
                    return {class_name: row["cnt"]}
                else:
                    rows = await conn.fetch(
                        "SELECT class_name, COUNT(*) as cnt FROM tiles GROUP BY class_name")
                    return {r["class_name"]: r["cnt"] for r in rows}
        except Exception as e:
            self._log(f"[DB] count_tiles error: {e}", level="error")
            return {}

    # ── Drag Challenges ─────────────────────────────────

    async def save_drag(self, class_name: str, target_class: str = "",
                        iframe_b64: str = "",
                        object_x: float = 0, object_y: float = 0,
                        target_x: float = 0, target_y: float = 0,
                        challenge_text: str = "",
                        success: bool = False,
                        solved_by: str = "") -> bool:
        """Save a drag challenge record."""
        if self._noop:
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO drag_challenges
                       (class_name, target_class, iframe_b64,
                        object_x, object_y, target_x, target_y,
                        challenge_text, success, solved_by)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                    class_name, target_class, iframe_b64,
                    object_x, object_y, target_x, target_y,
                    challenge_text, success, solved_by)
                await conn.execute(
                    """INSERT INTO knowledge (class_name, category,
                                              sample_count, last_seen_at)
                       VALUES ($1, 'drag', 1, NOW())
                       ON CONFLICT (class_name)
                       DO UPDATE SET sample_count = knowledge.sample_count + 1,
                                     last_seen_at = NOW()""",
                    class_name)
            return True
        except Exception as e:
            self._log(f"[DB] save_drag error: {e}", level="error")
            return False

    # ── Slider Challenges ────────────────────────────────

    async def save_slider(self, screenshot_b64: str = "",
                          offset_px: int = 0,
                          challenge_text: str = "",
                          success: bool = False) -> bool:
        """Save a slider challenge record."""
        if self._noop:
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO slider_records
                       (screenshot_b64, offset_px, challenge_text, success)
                       VALUES ($1,$2,$3,$4)""",
                    screenshot_b64, offset_px, challenge_text, success)
            return True
        except Exception as e:
            self._log(f"[DB] save_slider error: {e}", level="error")
            return False

    # ── Knowledge Queries ────────────────────────────────

    async def get_known_classes(self, min_samples: int = 5) -> list[str]:
        """Get classes with enough samples to train on."""
        if self._noop:
            return []
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT class_name FROM knowledge
                       WHERE sample_count >= $1
                       ORDER BY sample_count DESC""",
                    min_samples)
                return [r["class_name"] for r in rows]
        except Exception as e:
            self._log(f"[DB] get_known_classes error: {e}", level="error")
            return []

    async def get_knowledge_summary(self) -> list[dict]:
        """Get summary of all known classes and sample counts."""
        if self._noop:
            return []
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT class_name, category, sample_count,
                              confidence, last_seen_at
                       FROM knowledge
                       ORDER BY sample_count DESC""")
                return [dict(r) for r in rows]
        except Exception as e:
            self._log(f"[DB] get_knowledge_summary error: {e}", level="error")
            return []

    # ── Export for Training ──────────────────────────────

    async def export_yolo_dataset(self, output_dir: str = "training_data",
                                  min_confidence: float = 0.0) -> dict:
        """Export all successful tiles as a YOLO-format dataset on disk.
        
        Creates:
            training_data/
              train/
                images/   - PNG files
                labels/   - YOLO .txt files
              valid/
                images/
                labels/
              data.yaml   - dataset config

        Returns dict with class counts.
        """
        if self._noop:
            self._log("[DB] No database — cannot export", level="error")
            return {}

        import io
        from pathlib import Path
        from PIL import Image

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT class_name, image_b64, id
                       FROM tiles
                       WHERE success = TRUE AND confidence >= $1
                       ORDER BY class_name, captured_at""",
                    min_confidence)
        except Exception as e:
            self._log(f"[DB] export query error: {e}", level="error")
            return {}

        if not rows:
            self._log("[DB] No tiles to export", level="warn")
            return {}

        # Build class map
        classes = sorted(set(r["class_name"] for r in rows))
        class_map = {c: i for i, c in enumerate(classes)}

        base = Path(output_dir)
        train_img_dir = base / "train" / "images"
        train_lbl_dir = base / "train" / "labels"
        valid_img_dir = base / "valid" / "images"
        valid_lbl_dir = base / "valid" / "labels"

        for d in [train_img_dir, train_lbl_dir, valid_img_dir, valid_lbl_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # Split 80/20 train/valid
        import random
        random.shuffle(rows)
        split = int(len(rows) * 0.8)
        train_rows = rows[:split]
        valid_rows = rows[split:]

        counts = {c: 0 for c in classes}

        def save_rows(rows_subset, img_dir, lbl_dir):
            for r in rows_subset:
                cls_name = r["class_name"]
                cls_id = class_map[cls_name]
                img_b64 = r["image_b64"]
                img_id = r["id"]

                counts[cls_name] = counts.get(cls_name, 0) + 1

                # Decode base64 to PNG
                try:
                    img_data = base64.b64decode(img_b64)
                    img = Image.open(io.BytesIO(img_data))
                    fname = f"{cls_name}_{img_id}.png"
                    img.save(str(img_dir / fname), "PNG")

                    # YOLO label: class_id x_center y_center width height
                    # For full-image tiles, the object fills most of the tile
                    # so we set bbox to cover ~80% of the image
                    w, h = img.size
                    cx, cy = 0.5, 0.5
                    bw, bh = 0.8, 0.8
                    with open(str(lbl_dir / fname.replace(".png", ".txt")), "w") as f:
                        f.write(f"{cls_id} {cx:.4f} {cy:.4f} {bw:.4f} {bh:.4f}\n")
                except Exception as e:
                    self._log(f"[DB] Export error for tile {img_id}: {e}",
                              level="warn")

        save_rows(train_rows, train_img_dir, train_lbl_dir)
        save_rows(valid_rows, valid_img_dir, valid_lbl_dir)

        # Create data.yaml
        yaml_lines = [
            f"# Auto-generated by KnowledgeDB on {datetime.now().isoformat()}",
            f"train: {output_dir}/train/images",
            f"val: {output_dir}/valid/images",
            "",
            f"nc: {len(classes)}",
            "names:",
        ]
        for c in classes:
            yaml_lines.append(f"  {class_map[c]}: {c}")

        with open(str(base / "data.yaml"), "w") as f:
            f.write("\n".join(yaml_lines))

        self._log(f"[DB] Exported {len(rows)} images to {output_dir}/ "
                  f"({len(classes)} classes: {', '.join(classes)})")
        return counts


# ── Standalone Testing ────────────────────────────────────

async def main():
    db = await KnowledgeDB.create()
    if db._noop:
        print("No DATABASE_URL set. Set it to test:")
        print("  export DATABASE_URL=postgres://user:pass@localhost:5432/captcha_db")
        return

    # Test save
    import base64
    test_b64 = base64.b64encode(b"fake_image_data").decode()
    await db.save_tile("star", test_b64, challenge="click all stars", confidence=0.95, success=True)
    await db.save_tile("bus", test_b64, challenge="select all buses", confidence=0.80, success=True)

    # Query
    tiles = await db.get_tiles("star")
    print(f"Star tiles: {len(tiles)}")

    summary = await db.get_knowledge_summary()
    print(f"Knowledge: {summary}")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
