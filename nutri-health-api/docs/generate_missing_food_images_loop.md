# Recommendation Food Image Pre-Generation

## Why this exists

The recommendation API enriches food items with images in this priority order:

1. **Metadata image** — `data/processed/clean_food_metadata.json` contains manually
   curated `image_url` values for many common foods.
2. **Generated image** — `data/image_cache/food_image_cache.json` records images that
   were previously generated via Pollinations AI and saved to
   `static/generated_foods/{food_key}.png`.
3. **Category fallback** — `static/category_fallback/{category}.png` is used as a
   last resort when no specific image is available.

At runtime the API queues a FastAPI `BackgroundTask` whenever a food item has no
specific image.  That approach works well locally but **fails reliably on Render**:

- Render's free-tier and standard deployments use ephemeral file systems.  Any file
  written at runtime is lost on the next deploy or restart.
- Background tasks run after the response is already sent, so there is no guarantee
  the task completes before the service is recycled.
- If the Pollinations API is slow or temporarily unavailable, the background task
  fails silently and the food item is permanently stuck showing only the category
  fallback image.

The solution is to **pre-generate images locally** and **commit the PNG files and
updated cache JSON** to the repository so they are deployed as static assets.

---

## Why category fallback does not count as a specific image

`static/category_fallback/{category}.png` images are generic placeholders (e.g. a
generic "fruits" image, a generic "grains" image).  They carry no information about
the individual food item and look identical for every food in the same category.
The script treats them as "not covered" and will always try to generate a real food
image even when a category fallback exists.

---

## Script location

```
nutri-health-api/scripts/generate_missing_food_images_loop.py
```

Run from the `nutri-health-api/` directory, or from any location — the script
resolves all paths relative to its own location.

---

## How to run

### Preview only (no network calls, no files written)

```bash
python scripts/generate_missing_food_images_loop.py --dry-run --limit 10
```

### Single pass over the default demo list

```bash
python scripts/generate_missing_food_images_loop.py --sleep 1.5
```

### Run until all foods are covered (unlimited rounds, retry failures)

```bash
python scripts/generate_missing_food_images_loop.py --max-rounds 0 --retry-failed --sleep 1
```

### Use a custom food list

```bash
python scripts/generate_missing_food_images_loop.py --input path/to/foods.json --verbose
```

The `--input` JSON may be any of:

- A list of strings: `["salmon", "plain yogurt"]`
- A list of objects: `[{"food_name": "salmon"}, {"name": "plain yogurt"}]`
- A recommendation API response object with buckets:
  ```json
  {
    "super_power_foods": [...],
    "tiny_hero_foods": [...],
    "try_less_foods": [...]
  }
  ```

### All CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--input PATH` | built-in list | JSON file with food names |
| `--limit N` | 0 (no limit) | Process at most N foods |
| `--dry-run` | off | Preview only; no files written |
| `--force` | off | Overwrite existing images and retry failed/pending |
| `--retry-failed` | off | Retry items previously marked failed |
| `--retry-pending` | off | Retry items currently marked pending |
| `--max-rounds N` | 1 | Loop rounds; 0 = unlimited |
| `--sleep SECONDS` | 1.0 | Pause between individual requests |
| `--round-sleep SECONDS` | 5.0 | Pause between rounds |
| `--timeout SECONDS` | 30 | HTTP request timeout |
| `--verbose` | off | Detailed per-food output |

---

## What to commit for delivery

After the script finishes, commit these two paths so Render serves the images from
static files on the next deploy:

```bash
git add static/generated_foods/
git add data/image_cache/food_image_cache.json
git commit -m "feat: pre-generate recommendation food images for delivery"
```

**Do not commit** `static/category_fallback/` changes unless you have intentionally
updated those placeholder images — they are separate from generated food images.

---

## Skip logic (what already counts as covered)

| Condition | Counts as covered? |
|-----------|-------------------|
| `data/processed/clean_food_metadata.json` has `image_url` for this food | Yes |
| `static/generated_foods/{food_key}.png` file exists on disk | Yes |
| Cache entry `image_status == "ready"` but PNG file is absent | No — regenerated |
| `static/category_fallback/{category}.png` exists | No — generic placeholder only |

---

## Safety guarantees

- **Never overwrites** an existing generated PNG unless `--force` is passed.
- **Never deletes** cache entries; only adds or updates them.
- **Never crashes** on a single-food failure; logs the error and continues.
- **Safe to stop and re-run** at any time — cache is saved after every item.
- **Idempotent** — running it twice produces the same result.
