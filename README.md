# Jenne Table Forge

An automated table parsing, text cleaning, and compilation pipeline for Foundry VTT (Version 13+). It extracts structured rollable tables from PDF documents and generates ready-to-use Foundry VTT JSON files.

## Features
- **PDF Extraction**: Uses Docling layout parsing to extract multi-column table content from PDFs.
- **Stage 2 (Flatten)**: Processes extracted columns and flattens horizontal/interleaved ranges.
- **Stage 3 (Clean)**: Sanitizes and standardizes text, filters out OCR anomalies, resolves sub-numbered lists, and merges description wraps.
- **Stage 4 (Slice)**: Automatically slices long/grouped tables into individual snippet files.
- **Stage 5 (Parse & Validate)**: Translates markdown table slices into structured JSON formats matching Foundry VTT rollable table schema.
- **Lock Protection**: Protects manually refined/customized files using `tableforge_mappings.json` locks (`clean_locked` and `locked`) to prevent scripts from overwriting custom edits.

## Workspace Layout
- `data/raw_markdown/`: Raw layout output from Stage 1.
- `data/flat_markdown/`: Flattened and zipped columns from Stage 2.
- `data/clean_markdown/`: Cleaned and standardized files from Stage 3.
- `data/extracted_tables/`: Sliced individual table snippets from Stage 4.
- `data/tables/`: Generated ready-to-use Foundry VTT JSON tables.

## Run Pipeline
Run the main script using the command-line options:
```bash
python scripts/python/tableforge.py --start-at <stage> --force
```
See the script help (`--help`) for all parameters and settings.
