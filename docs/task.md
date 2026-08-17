# TableForge Upgrade Checklist

## Phase 1: Reorganization & Duplicates Purge
- `[x]` Update `.env` and script default path mappings to "Random Tables"
- `[x]` Remove duplicate book `greatbookofrandomtables(shorter).pdf` from `tableforge_tracker.json`
- `[x]` Purge all duplicate table JSON files from the Foundry module directory
- `[x]` Rename the local workspace folder from "Random (Tables, etc)" to "Random Tables" (Completed & Mapped)

## Phase 2: Python Compiler Enhancements (`tableforge.py`)
- `[x]` Add natural theme subfolder classification logic (`downtime`, `encounters`, `items`, `magic`, `monsters`, `npcs`, `world_building`, `running_the_game`)
- `[x]` Upgrade the visual Gemini prompt to capture true table headers (clean titles) and surrounding context (descriptions/modifiers)
- `[x]` Write the captured table `"description"` directly into the Foundry RollTable schema JSON output
- `[x]` Restructure the file writer to export tables into `tables/combined/<theme>/` and `tables/individual/<source_pdf>/`

## Phase 3: Client Importer Upgrades (`importer.js` & `importer.html`)
- `[x]` Upgrade jQuery button injection in `importer.js` to target `.header-actions.action-buttons` for v14 robust support
- `[x]` Implement Option B in `importer.js`:
  - `[x]` Add UI toggle/button for "Import to Compendium Pack" in `importer.html`
  - `[x]` Programmatically check and create a custom World-level Compendium Pack (`TableForge: Extracted Tables`)
  - `[x]` Programmatically build nested folder structures inside that compendium pack matching our disk themes/sources
  - `[x]` Populate the compendium pack with all selected tables, leaving the sidebar clean and performant

## Phase 4: Migration & Verification
- `[x]` Verify that the generated module functions perfectly inside Foundry VTT v14
- `[x]` Implement dynamic multi-tier Category and Source Book sidebar filters for navigating thousands of tables
- `[x]` Overhaul dialog layout geometry and flex auto-sizing to eliminate clipping and double scrollbars
- `[x]` Preserve checkbox selections during table preview clicks
- `[x]` Update `walkthrough.md` with final architecture and instructions

## Phase 5: Model Sunset Recovery & Chunking Robustness
- `[x]` Restore model to `gemini-3.1-flash-lite` to recover from deprecation errors and lower API costs
- `[x]` Implement CLI `--clean` flag to wipe progress tracker and outputs on demand
- `[x]` Align incremental and module writers to prevent suffix mismatch duplicate files on disk
- `[x]` Refactor `chunk_markdown` to do line-based splitting, preventing list chunk overflow duplication
- `[x]` Inject source PDF context to prompt for consistent chunk table naming
- `[x]` Implement advanced range-and-prefix deduplication to eliminate chunk overlap duplicates completely
- `[x]` Hide individual fragments of merged tables from the manifest to prevent importer clutter
