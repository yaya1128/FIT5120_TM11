#!/usr/bin/env python3
"""
generate_missing_food_images_loop.py

Offline pre-generation of missing recommendation food images.

Keeps looping over a food-name list until every item has a specific food image
(either from clean_food_metadata.json or from static/generated_foods/), or until
--max-rounds is reached, or the user presses Ctrl+C.

This script is designed to run locally before deployment so the generated PNG
files can be committed and served as reliable static assets on Render, instead
of relying on runtime background tasks that may not persist across restarts.

Usage:
    python scripts/generate_missing_food_images_loop.py --dry-run --limit 10
    python scripts/generate_missing_food_images_loop.py --max-rounds 0 --retry-failed --sleep 1
    python scripts/generate_missing_food_images_loop.py --input my_foods.json --verbose

What to commit after running:
    static/generated_foods/*.png
    data/image_cache/food_image_cache.json
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths  (resolved relative to this script's location: nutri-health-api/)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

METADATA_FILE  = BASE_DIR / "data" / "processed" / "clean_food_metadata.json"
CACHE_FILE     = BASE_DIR / "data" / "image_cache" / "food_image_cache.json"
GENERATED_DIR  = BASE_DIR / "static" / "generated_foods"
FALLBACK_DIR   = BASE_DIR / "static" / "category_fallback"

# ---------------------------------------------------------------------------
# Try to import existing service helpers; fall back to inline implementations
# when the modules do not exist yet.
#
# Matching signatures expected:
#   normalize_food_key(food_name: str) -> str
#   _build_pollinations_url(food_name: str) -> str
#   load_cache() -> dict
#   save_cache(cache: dict) -> None
#   infer_category(food_name: str) -> str
#   find_existing_image(food_name: str) -> Optional[str]
# ---------------------------------------------------------------------------
sys.path.insert(0, str(BASE_DIR))

_svc_normalize    = None
_svc_build_url    = None
_svc_load_cache   = None
_svc_save_cache   = None
_svc_infer_cat    = None
_svc_find_image   = None

try:
    from app.services.food_image_cache import normalize_food_key as _svc_normalize       # type: ignore
    from app.services.food_image_cache import load_cache         as _svc_load_cache       # type: ignore
    from app.services.food_image_cache import save_cache         as _svc_save_cache       # type: ignore
    try:
        from app.services.food_image_cache import _build_pollinations_url as _svc_build_url  # type: ignore
    except ImportError:
        pass
except ImportError:
    pass

try:
    from app.services.enrichment import infer_category as _svc_infer_cat   # type: ignore
except ImportError:
    pass

try:
    from app.services.food_metadata import find_existing_image as _svc_find_image  # type: ignore
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Inline implementations
# ---------------------------------------------------------------------------

# Keyword map for category inference.  Order matters: more specific first.
_CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "seafood":     ["salmon", "tuna", "sardine", "shrimp", "prawn", "crab", "lobster",
                    "fish ball", "fish", "cod", "tilapia", "mackerel", "herring",
                    "oyster", "clam", "scallop", "squid", "octopus", "anchov"],
    "dairy":       ["milk", "yogurt", "yoghurt", "cheese", "butter", "cream",
                    "cottage cheese", "sour cream", "whey", "kefir"],
    "proteins":    ["chicken", "beef", "pork", "turkey", "lamb", "duck", "egg",
                    "tofu", "tempeh", "edamame", "lentil", "chickpea",
                    "kidney bean", "black bean", "soybean"],
    "fruits":      ["apple", "banana", "mango", "orange", "kiwi", "blueberr",
                    "strawberr", "grape", "peach", "pear", "watermelon",
                    "pineapple", "cherry", "lemon", "lime", "papaya", "avocado",
                    "melon", "plum", "apricot"],
    "vegetables":  ["carrot", "broccoli", "spinach", "kale", "lettuce", "tomato",
                    "cucumber", "pepper", "onion", "garlic", "sweet potato",
                    "potato", "corn", "celery", "cauliflower", "pea", "green bean",
                    "asparagus", "zucchini", "mushroom", "eggplant", "cabbage",
                    "radish", "beet", "leek"],
    "grains":      ["rice", "oat", "oatmeal", "bread", "noodle", "pasta", "wheat",
                    "barley", "quinoa", "millet", "rye", "tortilla", "whole grain",
                    "cereal", "granola", "porridge", "cracker", "toast"],
    "bakery":      ["croissant", "bagel", "bun", "scone", "roll", "muffin",
                    "pancake", "waffle"],
    "sweets":      ["candy", "chocolate", "cake", "cookie", "ice cream", "donut",
                    "pie", "pastry", "brownie", "jam", "syrup", "pudding",
                    "marshmallow", "lollipop", "caramel", "sweet"],
    "snacks":      ["chip", "crisp", "popcorn", "pretzel", "french fr", "nachos",
                    "potato chip"],
    "beverages":   ["juice", "smoothie", "shake", "drink", "soda", "cola", "tea",
                    "coffee", "lemonade"],
    "processed":   ["sausage", "hot dog", "bacon", "ham", "nugget", "fish ball",
                    "instant noodle", "canned", "frozen", "fried chicken"],
}


def _inline_normalize_food_key(food_name: str) -> str:
    """Lowercase, replace non-alphanumeric runs with underscore, strip edges."""
    key = food_name.strip().lower()
    key = re.sub(r"[^a-z0-9]+", "_", key)
    return key.strip("_")


def _inline_infer_category(food_name: str) -> str:
    """Return a broad category string inferred from keyword matching."""
    name_lower = food_name.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in name_lower:
                return category
    return "general"


def _inline_build_pollinations_url(food_name: str) -> str:
    """Build a Pollinations AI image-generation URL for the given food name.

    Uses the simple /prompt/{text} endpoint without an explicit model parameter.
    Specifying model=flux directly triggers stricter rate-limiting (HTTP 402);
    the default endpoint routes to the same model with a more permissive quota.
    """
    prompt = (
        f"professional food photography of {food_name}, "
        "appetizing, clean white background, natural lighting, high quality, "
        "isolated food item"
    )
    encoded = urllib.parse.quote(prompt)
    return f"https://image.pollinations.ai/prompt/{encoded}?nologo=true&width=512&height=512"


def _inline_load_cache() -> Dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _inline_save_cache(cache: Dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


# Metadata is loaded once and kept in memory for the lifetime of the script.
_metadata_store: Optional[Dict] = None


def _inline_find_existing_image(food_name: str) -> Optional[str]:
    """
    Return an image_url from clean_food_metadata.json if one exists for this
    food name, or None.  Metadata images count as already-covered; generated
    images in this function do NOT need to be re-generated.
    """
    global _metadata_store
    if _metadata_store is None:
        if not METADATA_FILE.exists():
            _metadata_store = {}
        else:
            try:
                with open(METADATA_FILE, "r", encoding="utf-8") as f:
                    _metadata_store = json.load(f)
            except Exception:
                _metadata_store = {}

    if not _metadata_store:
        return None

    name_lower = food_name.lower().strip()
    for key, entry in _metadata_store.items():
        if isinstance(entry, dict):
            entry_name = entry.get("food_name", key).lower().strip()
            if entry_name == name_lower or key.lower() == name_lower:
                url = entry.get("image_url", "")
                if url and url.strip():
                    return url.strip()
        elif isinstance(entry, str):
            # flat  {food_name: image_url}  format
            if key.lower() == name_lower and entry.strip():
                return entry.strip()
    return None


# ---------------------------------------------------------------------------
# Resolve helpers: prefer service module, fall back to inline
# ---------------------------------------------------------------------------
normalize_food_key     = _svc_normalize   or _inline_normalize_food_key
build_pollinations_url = _svc_build_url   or _inline_build_pollinations_url
load_cache             = _svc_load_cache  or _inline_load_cache
save_cache_to_disk     = _svc_save_cache  or _inline_save_cache
infer_category         = _svc_infer_cat   or _inline_infer_category
find_existing_image    = _svc_find_image  or _inline_find_existing_image


# ---------------------------------------------------------------------------
# Default demo food list
# ---------------------------------------------------------------------------
DEFAULT_FOODS: List[str] = [
    # Dairy
    "milk", "plain yogurt", "cottage cheese", "cheese",
    # Proteins
    "chicken breast", "beef", "turkey", "tofu",
    # Seafood
    "salmon", "tuna", "sardine",
    # Vegetables
    "carrot", "broccoli", "spinach", "kale", "sweet potato",
    # Fruits
    "mango", "apple", "banana", "blueberries", "orange", "kiwi",
    # Grains / complex carbs
    "brown rice", "oatmeal", "whole grain bread", "noodles with vegetables",
    # Legumes
    "lentils", "chickpeas", "edamame",
    # Processed / treat foods (score 1-2 in scan)
    "fried chicken", "instant noodles", "sausage", "hot dog", "fish balls",
    "chips", "candy", "ice cream", "sweetened fruit juice",
    "canned fruit in syrup", "croissant", "white toast with jam",
    "potato chips", "french fries",
]


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def _extract_names_from_list(items: list) -> List[str]:
    names: List[str] = []
    for item in items:
        if isinstance(item, str):
            n = item.strip()
            if n:
                names.append(n)
        elif isinstance(item, dict):
            for key in ("food_name", "food", "name"):
                val = item.get(key, "")
                if isinstance(val, str) and val.strip():
                    names.append(val.strip())
                    break
    return names


def load_food_names_from_file(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Plain list of strings or objects
    if isinstance(data, list):
        return _extract_names_from_list(data)

    if isinstance(data, dict):
        # Recommendation API response with named buckets
        names: List[str] = []
        for bucket in ("super_power_foods", "tiny_hero_foods", "try_less_foods"):
            if isinstance(data.get(bucket), list):
                names.extend(_extract_names_from_list(data[bucket]))
        if names:
            return names
        # Generic dict: pull names from any list values
        for v in data.values():
            if isinstance(v, list):
                names.extend(_extract_names_from_list(v))
        return names

    raise ValueError(f"Unsupported JSON format in {path}")


# ---------------------------------------------------------------------------
# Image download
# ---------------------------------------------------------------------------

def download_image(url: str, timeout: int, verbose: bool) -> bytes:
    """Download from url, validate it looks like an image, return raw bytes."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "NutriHealthBot/1.0 (food-image-pregen; offline)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content_type = resp.headers.get("Content-Type", "")
        data = resp.read()

    if "image" not in content_type.lower():
        raise ValueError(
            f"Unexpected Content-Type: {content_type!r} — server may have returned an error page"
        )
    if len(data) < 1024:
        raise ValueError(
            f"Response too small ({len(data)} bytes) — download may have failed silently"
        )

    if verbose:
        print(f"    Downloaded {len(data):,} bytes (Content-Type: {content_type.strip()})")
    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fallback_url(category: str) -> str:
    return f"/static/category_fallback/{category}.png"


def _is_covered(food_name: str) -> bool:
    """Return True if this food already has a specific image (not just category fallback)."""
    if find_existing_image(food_name):
        return True
    food_key = normalize_food_key(food_name)
    return (GENERATED_DIR / f"{food_key}.png").exists()


# ---------------------------------------------------------------------------
# Per-food processing
# ---------------------------------------------------------------------------

def process_food(
    food_name: str,
    cache: Dict,
    *,
    dry_run: bool,
    force: bool,
    retry_failed: bool,
    retry_pending: bool,
    timeout: int,
    verbose: bool,
) -> str:
    """
    Evaluate and possibly generate an image for one food name.

    Mutates `cache` in-place on success or failure.
    Returns one of:
        "skipped_metadata" | "skipped_exists" | "skipped_pending"
        "skipped_failed"   | "generated"      | "failed"
    """
    food_key = normalize_food_key(food_name)
    category = infer_category(food_name)
    out_file = GENERATED_DIR / f"{food_key}.png"
    entry = cache.get(food_key, {})

    if verbose:
        print(f"  [{food_name}]  key={food_key}  category={category}")

    # ---- Priority 1: existing metadata image --------------------------------
    existing = find_existing_image(food_name)
    if existing:
        if verbose:
            print(f"    -> skip: metadata image  {existing}")
        return "skipped_metadata"

    # ---- Priority 2: generated file already on disk -------------------------
    if out_file.exists() and not force:
        if verbose:
            print(f"    -> skip: file exists  {out_file.name}")
        # Keep cache consistent
        if entry.get("image_status") != "ready":
            cache[food_key] = {
                "food_name":    food_name,
                "category":     category,
                "image_url":    f"/static/generated_foods/{food_key}.png",
                "image_status": "ready",
                "created_at":   entry.get("created_at", _utcnow()),
                "updated_at":   _utcnow(),
                "error":        None,
            }
        return "skipped_exists"

    # ---- Cache state checks -------------------------------------------------
    status = entry.get("image_status")

    if status == "ready" and verbose:
        # Cache says ready but file is absent — will regenerate
        print(f"    Cache says ready but file missing — regenerating")

    if status == "failed" and not (retry_failed or force):
        if verbose:
            print(f"    -> skip: previous failure recorded (--retry-failed to retry)")
        return "skipped_failed"

    if status == "pending" and not (retry_pending or force):
        if verbose:
            print(f"    -> skip: status is pending (--retry-pending to retry)")
        return "skipped_pending"

    # ---- Dry run ------------------------------------------------------------
    if dry_run:
        url_preview = build_pollinations_url(food_name)[:72]
        print(f"  [DRY-RUN] Would generate: {food_name!r}  ->  {out_file.name}")
        if verbose:
            print(f"            URL: {url_preview}...")
        return "generated"

    # ---- Generate -----------------------------------------------------------
    url = build_pollinations_url(food_name)
    if verbose:
        print(f"    Fetching: {url[:80]}...")

    try:
        image_bytes = download_image(url, timeout=timeout, verbose=verbose)
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        out_file.write_bytes(image_bytes)

        cache[food_key] = {
            "food_name":    food_name,
            "category":     category,
            "image_url":    f"/static/generated_foods/{food_key}.png",
            "image_status": "ready",
            "created_at":   entry.get("created_at", _utcnow()),
            "updated_at":   _utcnow(),
            "error":        None,
        }
        print(f"  [OK] {food_name}  ->  {out_file.name}")
        return "generated"

    except Exception as exc:
        err = str(exc)
        print(f"  [FAIL] {food_name}: {err}")
        cache[food_key] = {
            "food_name":    food_name,
            "category":     category,
            "image_url":    entry.get("image_url") or _fallback_url(category),
            "image_status": "failed",
            "created_at":   entry.get("created_at", _utcnow()),
            "updated_at":   _utcnow(),
            "error":        err,
        }
        return "failed"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    # --- Load food names -----------------------------------------------------
    if args.input:
        try:
            foods = load_food_names_from_file(args.input)
            print(f"Loaded {len(foods)} food name(s) from: {args.input}")
        except Exception as exc:
            print(f"ERROR loading input file: {exc}")
            sys.exit(1)
    else:
        foods = list(DEFAULT_FOODS)
        print(f"Using built-in default demo list ({len(foods)} foods)")

    # Apply limit
    if args.limit and args.limit > 0:
        foods = foods[: args.limit]
        print(f"Limited to first {len(foods)} food(s)")

    # Deduplicate while preserving order
    seen: set = set()
    deduped: List[str] = []
    for name in foods:
        key = name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(name)
    foods = deduped
    print(f"Unique foods to consider: {len(foods)}")

    if args.dry_run:
        print("[DRY-RUN MODE]  No files will be written.\n")

    max_rounds        = args.max_rounds  # 0 = unlimited
    batch_size        = args.batch_size  # 0 = no cap per round
    round_num         = 0
    consecutive_zero  = 0               # rounds in a row with 0 generated
    cache: Dict       = {}

    try:
        while True:
            round_num += 1
            if max_rounds > 0 and round_num > max_rounds:
                print(f"\nReached --max-rounds={max_rounds}. Stopping.")
                break

            print(f"\n{'='*58}")
            label = f"{round_num}/{max_rounds}" if max_rounds > 0 else f"{round_num} (unlimited)"
            print(f"ROUND {label}")
            print(f"{'='*58}")

            # Reload cache at the start of each round to pick up any writes from the
            # previous round (safe to re-run; cache entries are never deleted here).
            cache = load_cache()

            # Decide which foods still need attention this round
            to_process: List[str] = []
            for food_name in foods:
                if _is_covered(food_name):
                    continue
                food_key = normalize_food_key(food_name)
                entry    = cache.get(food_key, {})
                status   = entry.get("image_status")
                if status == "failed"  and not (args.retry_failed  or args.force):
                    continue
                if status == "pending" and not (args.retry_pending or args.force):
                    continue
                to_process.append(food_name)

            if not to_process:
                print("Nothing to generate — all food images are already covered.")
                break

            # Optionally cap how many items we attempt per round.  A small batch
            # size lets Pollinations recover between rounds without flooding it.
            if batch_size > 0 and len(to_process) > batch_size:
                to_process = to_process[:batch_size]
                print(f"Capped to --batch-size={batch_size} this round.")

            print(f"Foods to process this round: {len(to_process)}\n")

            counts = {
                "skipped_metadata": 0,
                "skipped_exists":   0,
                "skipped_pending":  0,
                "skipped_failed":   0,
                "generated":        0,
                "failed":           0,
            }

            for idx, food_name in enumerate(to_process, 1):
                if not args.verbose:
                    print(f"  [{idx}/{len(to_process)}] {food_name}", end=" ... ", flush=True)

                result = process_food(
                    food_name,
                    cache,
                    dry_run=args.dry_run,
                    force=args.force,
                    retry_failed=args.retry_failed,
                    retry_pending=args.retry_pending,
                    timeout=args.timeout,
                    verbose=args.verbose,
                )
                counts[result] = counts.get(result, 0) + 1

                if not args.verbose:
                    print(result)

                # Persist cache after every item so a Ctrl+C never loses progress
                if not args.dry_run:
                    save_cache_to_disk(cache)

                # Rate-limit between requests (skip after the last item)
                if idx < len(to_process) and args.sleep > 0:
                    time.sleep(args.sleep)

            # --- Round summary -----------------------------------------------
            print(f"\n--- Round {round_num} summary ---")
            print(f"  Skipped (metadata image):       {counts['skipped_metadata']}")
            print(f"  Skipped (generated file exists):{counts['skipped_exists']}")
            print(f"  Skipped (pending):              {counts['skipped_pending']}")
            print(f"  Skipped (previously failed):    {counts['skipped_failed']}")
            print(f"  Generated:                      {counts['generated']}")
            print(f"  Failed:                         {counts['failed']}")

            still_missing = [f for f in foods if not _is_covered(f)]
            print(f"  Still missing:                  {len(still_missing)}")
            if still_missing and args.verbose:
                for name in still_missing:
                    print(f"    - {name}")

            if not still_missing:
                print("\nAll food images are now covered!")
                break

            # Track consecutive rounds with zero progress.  Stop only after 3 in a
            # row so a temporary Pollinations outage doesn't end the run prematurely.
            attempted = counts["generated"] + counts["failed"]
            if attempted > 0 and counts["generated"] == 0:
                consecutive_zero += 1
                print(
                    f"\n  [warn] No images generated this round "
                    f"({consecutive_zero}/3 consecutive zero-progress rounds)."
                )
                if consecutive_zero >= 3:
                    print(
                        "  Stopping after 3 consecutive rounds with zero progress.\n"
                        "  Check network / Pollinations status, then re-run."
                    )
                    break
            else:
                consecutive_zero = 0

            # Sleep between rounds (only if there will be another round)
            if (max_rounds == 0 or round_num < max_rounds) and args.round_sleep > 0:
                print(f"\nWaiting {args.round_sleep}s before next round...")
                time.sleep(args.round_sleep)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user (Ctrl+C).")
        if not args.dry_run:
            save_cache_to_disk(cache)
            print("Cache saved.")

    # --- Final summary -------------------------------------------------------
    print(f"\n{'='*58}")
    print("FINAL SUMMARY")
    print(f"{'='*58}")
    covered  = [f for f in foods if _is_covered(f)]
    missing  = [f for f in foods if not _is_covered(f)]
    print(f"Total foods considered:          {len(foods)}")
    print(f"Covered (metadata or generated): {len(covered)}")
    print(f"Still missing:                   {len(missing)}")
    if missing:
        print("Foods still missing specific images:")
        for name in missing:
            print(f"  - {name}")

    if not args.dry_run and missing:
        print(
            "\nTip: re-run with --max-rounds 0 --retry-failed to keep trying "
            "until all images are covered."
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_missing_food_images_loop.py",
        description=(
            "Offline pre-generation of recommendation food images. "
            "Loops until all foods are covered or --max-rounds is reached."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview what would be generated (no network calls, no writes):
  python scripts/generate_missing_food_images_loop.py --dry-run --limit 10

  # Run one pass over the default list:
  python scripts/generate_missing_food_images_loop.py --sleep 1.5

  # Run until all are covered, retrying failures:
  python scripts/generate_missing_food_images_loop.py --max-rounds 0 --retry-failed --sleep 1

  # Use a custom food list:
  python scripts/generate_missing_food_images_loop.py --input my_foods.json --verbose
""",
    )
    parser.add_argument(
        "--input", metavar="PATH",
        help="JSON file with food names (list of strings, list of objects, "
             "or recommendation API response). Defaults to built-in demo list.",
    )
    parser.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="Consider at most N foods from the list total (0 = no limit, default: 0).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=0, metavar="N",
        help="Attempt at most N items per round (0 = no cap, default: 0). "
             "Useful to avoid triggering Pollinations rate-limits: try --batch-size 5.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done; no files are written.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing generated images and re-attempt failed/pending items.",
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Retry food items that previously failed.",
    )
    parser.add_argument(
        "--retry-pending", action="store_true",
        help="Retry food items currently marked as pending in the cache.",
    )
    parser.add_argument(
        "--max-rounds", type=int, default=1, metavar="N",
        help="Maximum loop rounds (0 = unlimited, default: 1).",
    )
    parser.add_argument(
        "--sleep", type=float, default=1.0, metavar="SECONDS",
        help="Seconds to sleep between individual requests (default: 1.0).",
    )
    parser.add_argument(
        "--round-sleep", type=float, default=5.0, metavar="SECONDS",
        help="Seconds to sleep between rounds (default: 5.0).",
    )
    parser.add_argument(
        "--timeout", type=int, default=30, metavar="SECONDS",
        help="HTTP request timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print detailed per-food output.",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args   = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
