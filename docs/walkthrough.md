# VTT-TableForge: Complete Walkthrough & Usage Guide

**VTT-TableForge** is a production-ready, highly automated utility that extracts random tables from RPG PDFs, normalizes typographical and spelling anomalies, semantically clusters related topics, compiles customized rarity weights, and packages them into a fully-formed FoundryVTT v14 compatible module featuring a premium in-world importer interface with native Compendium pack organization.

---

## 1. System Architecture

The solution uses a hybrid backend/frontend architecture designed to overcome browser security sandboxes and heavy PDF parsing overhead:

```mermaid
graph TD
    subgraph Local Workspace (Random Tables)
        A[D&D PDFs] --> B[tableforge.py]
        C[weight_config.json] --> B
    end
    
    subgraph Compilation & Restructuring
        B -->|1. High-Fidelity OCR & Description| E[Surrounding Rules + Clean Title]
        B -->|2. Theme Subfolder Grouping| F[tables/combined/theme/]
        B -->|3. PDF Folder Separation| G[tables/individual/pdf_folder/]
        B -->|4. Purge Duplicates| H[Delete Shorter/Deleted PDF Data]
    end
    
    subgraph Foundry VTT v14 Module
        E & F & G --> I[C:/FoundryVtt/Data/modules/vtt-tableforge]
        I -->|Inject Button| J[RollTable Sidebar Tab]
    end
    
    subgraph Option B Compendium Import
        J -->|Open Importer UI| K[Glassmorphic Dialog]
        K -->|Select Import to Compendium| L[World Compendium Created]
        L -->|Dynamic Folder Creation| M[Folders in Compendium]
        M -->|Populate Tables| N[Tables Organized inside Pack]
    end
```

### Components Created (C: Drive Setup):
1. **[tableforge.py](file:///C:/FoundryVtt/Data/modules/jenne-table-forge-importer/scripts/python/tableforge.py)**: The Python 3 extraction compiler.
2. **[weight_config.json](file:///C:/FoundryVtt/Data/modules/jenne-table-forge-importer/scripts/python/weight_config.json)**: Rarity weight profiles configuration.
3. **[tableforge_tracker.json](file:///C:/FoundryVtt/Data/modules/jenne-table-forge-importer/scripts/python/tableforge_tracker.json)**: The incremental process state database.
4. **[requirements.txt](file:///C:/FoundryVtt/Data/modules/jenne-table-forge-importer/scripts/python/requirements.txt)**: Python package dependencies.
5. **[tableforge.env](file:///C:/FoundryVtt/Data/modules/jenne-table-forge-importer/scripts/python/tableforge.env)**: Environment configuration.
6. **[FoundryVTT Module](file:///C:/FoundryVtt/Data/modules/jenne-table-forge-importer)**: The installable module folder.

---

## 2. Directory Hierarchy and Sanitization Design

All table JSON files inside the module's `data/tables/` folder are organized cleanly:

### Folder Hierarchy
* **`tables/combined/<theme>/`**: Consolidated "Master Tables" grouped into D&D theme subfolders matching your sidebar directories:
  * `downtime/`, `encounters/`, `items/`, `magic/`, `monsters/`, `npcs/`, `world_building/`, `running_the_game/`.
  * *Filenames:* `master_tavern_insults_combined.json`.
* **`tables/individual/<source_pdf_folder>/`**: Original individual tables grouped into subfolders named after their source book PDF:
  * *Filenames:* Sanitized table titles with starting page suffix, e.g. `100_tavern_insults_p2.json`.

---

## 3. Dynamic Chunking & Overlap Deduplication

To prevent multi-page tables from creating duplicate entries or fragmenting, the compiler uses a two-tier approach:
1. **Line-based Chunking:** Splitting raw Markdown at line boundaries (`\n`) using a size limit (~12,000 characters). This keeps chunks small and mathematically bounded.
2. **Context Injection:** Injecting the source PDF title as a hint to Gemini, ensuring all chunk extractions from the same document share a consistent table name.
3. **Advanced Deduplication:** Merging chunks programmatically and resolving duplicates by both roll range ranges (within the same PDF) and word-prefix fuzzy matching (e.g. `Stone Brew` matching `Stone Brew: A mixture`).
4. **Clean Manifest:** Excluding fragmented individual files from `metadata.json` if they were successfully merged into a master table, keeping the importer sidebar free of junk.

---

## 4. Run commands & Debug Options

You can run the compiler globally from any directory (since `tableforge` wrapper is in your system `PATH`):

### Normal Incremental Scan (Skip already processed files):
```powershell
tableforge --incremental
```

### Fresh Debug Sweep (Wipe tracker and outputs, re-process everything):
```powershell
tableforge --clean
```

---

## 6. How to Use in Foundry VTT v14

Once the python script completes, the VTT-TableForge module is instantly deployed directly to your Foundry user data folder.

### Step 1: Enable the Module
1. **No URL installation is needed!** Because the files are placed directly on your disk, the module is already pre-installed.
2. Launch Foundry VTT and open your D&D 5e Game World.
3. Navigate to **Game Settings** -> **Manage Modules**.
4. Locate **"VTT TableForge: Extracted D&D Tables"** and check the box to enable it. Save and Reload.

### Step 2: Open the TableForge Importer UI
1. Go to the **Rollable Tables** sidebar tab.
2. In the header action bar, click the new **"TableForge Importer"** button (styled in a premium purple-magenta gradient).

### Step 3: Choose Your Import Target & Import (Option B - Recommended)
The premium **Dark Mode Glassmorphic Dialog** will open:
1. **Search and Filter**: Type in the top search box to instantly search tables or PDFs.
2. **Live Preview**: Click any table to see a preview of its results, ranges, and context rules.
3. **Select Tables**: Bulk select via "All" or tick specific checkboxes.
4. **Choose Option B (Compendium Pack - Recommended)**:
   * Under the **Import Target** dropdown at the bottom, select **"Compendium Pack (Option B - Clean)"**.
   * Click **Import Selected**.
   * The dialog will automatically create a custom World-level Compendium Pack (`TableForge: Extracted Tables`), build the matching folder tree *inside* that pack (e.g. `TableForge (Individual) -> Advanced Weather Table`), and populate it with all selected tables.
   * This leaves your sidebar clean, performant, and clutter-free!

---

## 7. Two-Tier Consolidation Architecture

To handle both long multi-page tables (which must be split into chunks during parsing) and cross-document duplicates (e.g. similar tables across different source books), the compiler implements a **two-tier consolidation flow**:

### Tier 1: Local PDF-Level Consolidation (Resolving Page Splits)
* **Goal:** Merges chunk fragments extracted from the *same PDF* into single, complete individual tables.
* **Process:** For each document, all extracted chunks are immediately grouped by title similarity. Matching tables are stitched together (deduplicated by original roll range and prefix), sorted by roll number, and written as a **single, unified individual table JSON file** (e.g., `100_fantasy_drugs_p1.json` or `items_in_a_wizard_s_chamber_p2.json`).
* **Outcome:** No more fragmented `_p1`, `_p2` file spam on disk! Every table in a PDF is exported as a single, complete rollable table file.

### Tier 2: Global Cross-PDF Consolidation (Merging the Library)
* **Goal:** Synthesizes consolidated "Master Tables" containing all entries from similar tables across different books, while preserving the individual source tables.
* **Process:** The script scans all clean individual tables in your tracker. Tables on similar subjects from different PDFs (e.g. `Items in a Wizard's Chamber` from `bookofrandomtables.pdf` and `Items on a Dead Adventurer` from `greatbookofrandomtables.pdf`) are merged into a single consolidated `"Master"` table.
* **Outcome:** The final Foundry VTT importer manifest includes:
  1. The **Consolidated Master Tables** (combining entries and deduplicating them across your entire library).
  2. The **Clean Individual Tables** (giving you the option to import a specific book's original table directly).
  3. Unique tables (which had no matching counterparts in other books) are preserved as individual tables.
