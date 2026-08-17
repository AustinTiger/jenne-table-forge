
const { ApplicationV2, HandlebarsApplicationMixin } = foundry.applications.api;

class TableForgeImporterDialog extends HandlebarsApplicationMixin(ApplicationV2) {
    static DEFAULT_OPTIONS = {
        id: "tableforge-importer-dialog",
        classes: ["tableforge-dialog"],
        window: {
            title: "VTT TableForge: PDF Table Importer",
            resizable: true
        },
        position: {
            width: 1000,
            height: 750
        }
    };

    static PARTS = {
        main: {
            template: "modules/jenne-table-forge/scripts/importer.html"
        }
    };

    constructor(options={}) {
        super(options);
        this.tables = [];
        this.selectedTables = new Set();
        this.activePreview = null;
        this.searchQuery = "";
        
        // Local Python Server states
        this.serverUrl = "http://localhost:8055";
        this.isServerConnected = false;
        this.isPipelineRunning = false;
        this.pollTimer = null;
        this.knownLogCount = 0;

        this.loadManifest();
    }

    async loadManifest() {
        try {
            const response = await fetch('/modules/jenne-table-forge/data/metadata.json');
            this.tables = await response.json();
            
            // Re-render window with new data if open
            if (this.rendered) {
                this.render(true);
            }
        } catch (e) {
            ui.notifications.error("Failed to load TableForge tables metadata!");
            console.error(e);
        }
    }

    async _prepareContext(options) {
        // Map tables to preserve active checklist selections during re-renders
        const tablesWithSelection = this.tables.map(t => ({
            ...t,
            checked: this.selectedTables.has(t.file)
        }));

        // Extract unique themes and PDFs for selection dropdown lists
        const themes = new Set();
        const pdfs = new Set();
        
        for (const t of this.tables) {
            const parts = t.file.split("/");
            if (parts[0] === "combined") {
                themes.add(parts[1]);
            } else {
                pdfs.add(t.source);
            }
        }
        
        const sortedThemes = Array.from(themes).sort().map(theme => ({
            value: `theme:${theme}`,
            label: theme.charAt(0).toUpperCase() + theme.slice(1).replace("_", " ")
        }));
        
        const sortedPDFs = Array.from(pdfs).sort().map(pdf => ({
            value: `pdf:${pdf}`,
            label: pdf.replace(".pdf", "").split("_").map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ")
        }));

        return {
            tables: tablesWithSelection,
            themes: sortedThemes,
            pdfs: sortedPDFs,
            selectedCount: this.selectedTables.size,
            activePreview: this.activePreview,
            searchQuery: this.searchQuery
        };
    }

    // High-performance real-time search & multiple dropdown filters
    applyFilters(html) {
        const query = (html.find(".tableforge-search-box").val() || "").toLowerCase();
        const category = html.find("#filter-category").val() || "all";
        const theme = html.find("#filter-theme").val() || "all";
        const source = html.find("#filter-source").val() || "all";

        html.find(".tableforge-item").each((idx, el) => {
            const tableMeta = this.tables[idx];
            if (!tableMeta) return;
            
            // 1. Text Search query
            const nameMatch = tableMeta.name.toLowerCase().includes(query);
            const srcMatch = tableMeta.source.toLowerCase().includes(query);
            const matchesSearch = nameMatch || srcMatch;
            
            // 2. Category Type (combined vs individual)
            const fileParts = tableMeta.file.split("/");
            const isMaster = tableMeta.is_master;
            const itemCategory = isMaster ? "combined" : "individual";
            const matchesCategory = (category === "all" || itemCategory === category);
            
            // 3. Theme filter
            let matchesTheme = false;
            if (theme === "all") {
                matchesTheme = true;
            } else if (itemCategory === "combined") {
                const itemTheme = fileParts[1];
                matchesTheme = (theme === `theme:${itemTheme}`);
            }

            // 4. Source PDF filter
            let matchesSource = false;
            if (source === "all") {
                matchesSource = true;
            } else if (itemCategory === "individual") {
                const pdfName = tableMeta.source;
                matchesSource = (source === `pdf:${pdfName}`);
            }
            
            // Dynamic evaluation
            if (matchesSearch && matchesCategory && matchesTheme && matchesSource) {
                $(el).show();
            } else {
                $(el).hide();
            }
        });
    }

    // Dynamic background connection status polling
    async pollServerStatus(html) {
        if (!this.rendered) return;

        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 800); // 800ms limit

            const response = await fetch(`${this.serverUrl}/status`, { signal: controller.signal });
            clearTimeout(timeoutId);
            
            const data = await response.json();
            this.isServerConnected = true;

            // Update GUI connection badge
            const badge = html.find("#pipeline-status");
            badge.removeClass("offline").addClass("online");
            badge.html(`<i class="fa-solid fa-circle-check"></i> Pipeline Connected`);

            // Enable control buttons
            html.find("#btn-pipeline-browse").prop("disabled", false);
            
            // Enforce compiler running logic states
            if (data.running) {
                this.isPipelineRunning = true;
                html.find("#btn-pipeline-execute").hide();
                html.find("#btn-pipeline-cancel").show();
                html.find("#pipeline-path").prop("disabled", true);
                html.find("#btn-pipeline-browse").prop("disabled", true);

                // Update real-time progress bar width
                html.find(".tableforge-progress-bar").css("width", `${data.progress}%`);
            } else {
                // If it just finished compiling, reload catalog checklists automatically
                if (this.isPipelineRunning) {
                    this.isPipelineRunning = false;
                    html.find("#btn-pipeline-execute").show();
                    html.find("#btn-pipeline-cancel").hide();
                    html.find("#pipeline-path").prop("disabled", false);
                    html.find("#btn-pipeline-browse").prop("disabled", false);
                    html.find(".tableforge-progress-bar").css("width", "0%");
                    
                    this.loadManifest();
                }
                
                html.find("#btn-pipeline-execute").prop("disabled", false);
                html.find("#btn-pipeline-execute").show();
                html.find("#btn-pipeline-cancel").hide();
            }

            // Sync scrolling status console outputs
            if (data.logs && data.logs.length !== this.knownLogCount) {
                const consoleLogs = html.find(".tableforge-console");
                consoleLogs.empty();
                data.logs.forEach(log => {
                    consoleLogs.append(`<p class="tableforge-log-entry ${log.type}">${log.message}</p>`);
                });
                consoleLogs.scrollTop(consoleLogs[0].scrollHeight);
                this.knownLogCount = data.logs.length;
            }

        } catch (err) {
            this.isServerConnected = false;
            this.isPipelineRunning = false;
            
            // Update GUI offline badge
            const badge = html.find("#pipeline-status");
            badge.removeClass("online").addClass("offline");
            badge.html(`<i class="fa-solid fa-circle-xmark"></i> Pipeline Offline`);

            // Disable controls
            html.find("#btn-pipeline-execute").prop("disabled", true);
            html.find("#btn-pipeline-execute").show();
            html.find("#btn-pipeline-cancel").hide();
            html.find("#btn-pipeline-browse").prop("disabled", true);
            html.find("#pipeline-path").prop("disabled", false);
        }
    }

    _onRender(context, options) {
        super._onRender(context, options);
        const html = $(this.element);
        
        // Start live python status polling
        this.pollServerStatus(html);
        if (this.pollTimer) clearInterval(this.pollTimer);
        this.pollTimer = setInterval(() => this.pollServerStatus(html), 1000);

        // Apply filters instantly on render
        this.applyFilters(html);
        
        html.find(".tableforge-item").on("click", async (e) => {
            // Avoid trigger conflict on checkboxes
            if (e.target.type === "checkbox" || $(e.target).hasClass("tableforge-checkbox")) {
                return;
            }
            
            const idx = $(e.currentTarget).data("index");
            const tableMeta = this.tables[idx];
            if (!tableMeta) return;
            
            try {
                const response = await fetch(`/modules/jenne-table-forge/data/tables/${tableMeta.file}`);
                this.activePreview = await response.json();
                this.render(true);
            } catch (err) {
                ui.notifications.error("Failed to load table preview!");
            }
        });

        html.find(".tableforge-checkbox").on("change", (e) => {
            const file = $(e.currentTarget).data("file");
            if (e.currentTarget.checked) {
                this.selectedTables.add(file);
            } else {
                this.selectedTables.delete(file);
            }
            html.find(".selected-count").text(this.selectedTables.size);
        });

        // 1. Text Search Input listener
        html.find(".tableforge-search-box").on("input", () => {
            this.applyFilters(html);
        });

        // Reset search query button
        html.find(".action-reset-search").on("click", () => {
            html.find(".tableforge-search-box").val("");
            this.applyFilters(html);
        });

        // 2. Category Dropdown Filter listener
        html.find("#filter-category").on("change", (e) => {
            const cat = e.currentTarget.value;
            const themeSelect = html.find("#filter-theme");
            const sourceSelect = html.find("#filter-source");
            
            // Reset children selectors to All
            themeSelect.val("all");
            sourceSelect.val("all");
            
            if (cat === "all") {
                themeSelect.find("option").show();
                sourceSelect.find("option").show();
                html.find("#filter-theme-container").show();
                html.find("#filter-source-container").show();
            } else if (cat === "combined") {
                themeSelect.find("option").show();
                sourceSelect.find("option").hide();
            } else if (cat === "individual") {
                themeSelect.find("option").hide();
                sourceSelect.find("option").show();
            }
            
            this.applyFilters(html);
        });

        // 3. Theme Filter listener
        html.find("#filter-theme").on("change", () => {
            this.applyFilters(html);
        });

        // 4. Source PDF Filter listener
        html.find("#filter-source").on("change", () => {
            this.applyFilters(html);
        });

        // Staging options
        html.find(".select-all-btn").on("click", () => {
            html.find(".tableforge-checkbox:visible").each((i, el) => {
                el.checked = true;
                this.selectedTables.add($(el).data("file"));
            });
            html.find(".selected-count").text(this.selectedTables.size);
        });

        html.find(".deselect-all-btn").on("click", () => {
            html.find(".tableforge-checkbox:visible").each((i, el) => {
                el.checked = false;
                this.selectedTables.delete($(el).data("file"));
            });
            html.find(".selected-count").text(this.selectedTables.size);
        });

        // Python Pipeline Browse Folder button
        html.find("#btn-pipeline-browse").on("click", async () => {
            try {
                const response = await fetch(`${this.serverUrl}/browse`);
                const data = await response.json();
                if (data.path) {
                    html.find("#pipeline-path").val(data.path);
                }
            } catch (err) {
                console.error("Browse Folder Dialog Error:", err);
            }
        });

        // Python Pipeline compiler Execution triggers
        html.find("#btn-pipeline-execute").on("click", async () => {
            const pdfDir = html.find("#pipeline-path").val() || "";
            if (!pdfDir.trim()) {
                return ui.notifications.warn("Please enter or browse for a valid PDF search directory!");
            }

            try {
                const response = await fetch(`${this.serverUrl}/run`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ "pdf_dir": pdfDir })
                });
                
                if (response.ok) {
                    ui.notifications.info("TableForge Extraction Pipeline started successfully in the background. Check logs!");
                    this.pollServerStatus(html);
                } else {
                    const errData = await response.json();
                    ui.notifications.error(`Pipeline launch failed: ${errData.error}`);
                }
            } catch (err) {
                ui.notifications.error("Connection error: Unable to contact local Python Server!");
            }
        });

        // Cancel Pipeline execution trigger
        html.find("#btn-pipeline-cancel").on("click", async () => {
            try {
                await fetch(`${this.serverUrl}/kill`, { method: "POST" });
                ui.notifications.info("Cancellation signal successfully dispatched to background thread.");
            } catch (err) {
                console.error(err);
            }
        });

        // Executing import DB database pipeline operations
        html.find(".btn-import-execute").on("click", async () => {
            if (this.selectedTables.size === 0) {
                ui.notifications.warn("No tables selected for import!");
                return;
            }

            const importTarget = html.find("#import-target").val() || "compendium";
            ui.notifications.info(`Importing ${this.selectedTables.size} tables to ${importTarget === 'compendium' ? 'Compendium' : 'Sidebar'}. Please wait...`);
            let count = 0;
            
            // Helper function to build folder path recursively in Sidebar
            async function getOrCreateSidebarFolder(pathParts) {
                let parentId = null;
                for (const part of pathParts) {
                    let folder = game.folders.find(f => f.name === part && f.type === "RollTable" && f.folder?.id === parentId);
                    if (!folder) {
                        folder = await Folder.create({
                            name: part,
                            type: "RollTable",
                            folder: parentId
                        });
                    }
                    parentId = folder.id;
                }
                return parentId;
            }

            // Helper function to get or create Compendium Folder hierarchy
            async function getOrCreateCompendiumFolder(compendium, pathParts) {
                let parentId = null;
                for (const part of pathParts) {
                    let folder = compendium.folders.find(f => f.name === part && f.folder?.id === parentId);
                    if (!folder) {
                        folder = await Folder.create({
                            name: part,
                            type: "RollTable",
                            folder: parentId
                        }, { pack: compendium.metadata.id });
                    }
                    parentId = folder.id;
                }
                return parentId;
            }

            // Get or create custom compendium if target is compendium
            let compendium = null;
            if (importTarget === "compendium") {
                const packName = "world.tableforge-extracted-tables";
                compendium = game.packs.get(packName);
                if (!compendium) {
                    compendium = await CompendiumCollection.createCompendium({
                        type: "RollTable",
                        label: "TableForge: Extracted Tables",
                        name: "tableforge-extracted-tables"
                    });
                }
            }
            
            for (const file of this.selectedTables) {
                try {
                    const response = await fetch(`/modules/jenne-table-forge/data/tables/${file}`);
                    const tableData = await response.json();
                    
                    const fileParts = file.split("/");
                    const category = fileParts[0]; // "individual" or "combined"
                    
                    let pathParts = [];
                    if (category === "combined") {
                        pathParts.push("TableForge (Combined)");
                        const theme = fileParts[1];
                        const themeCap = theme.charAt(0).toUpperCase() + theme.slice(1).replace("_", " ");
                        pathParts.push(themeCap);
                    } else {
                        pathParts.push("TableForge (Individual)");
                        const pdfName = fileParts[1];
                        const pdfCap = pdfName.split("_").map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
                        pathParts.push(pdfCap);
                    }

                    if (importTarget === "sidebar") {
                        // 1. Sidebar Directory Import
                        const folderId = await getOrCreateSidebarFolder(pathParts);
                        tableData.folder = folderId;

                        let existing = game.tables.find(t => t.name === tableData.name);
                        if (existing) {
                            await existing.delete();
                        }
                        
                        await RollTable.create(tableData);
                    } else {
                        // 2. Compendium Pack Import
                        const folderId = await getOrCreateCompendiumFolder(compendium, pathParts);
                        tableData.folder = folderId;

                        let existing = compendium.index.find(entry => entry.name === tableData.name);
                        if (existing) {
                            const doc = await compendium.getDocument(existing._id);
                            await doc.delete();
                        }

                        await RollTable.create(tableData, { pack: compendium.metadata.id });
                    }
                    count++;
                } catch (err) {
                    console.error(`Failed to import table ${file}:`, err);
                }
            }
            
            ui.notifications.active = true;
            if (importTarget === "sidebar") {
                ui.notifications.info(`Successfully imported ${count} tables into organized folders inside the Rollable Tables tab!`);
            } else {
                ui.notifications.info(`Successfully imported ${count} tables into the 'TableForge: Extracted Tables' Compendium Pack!`);
            }
            this.close();
        });
    }

    _onClose(options={}) {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
        super._onClose(options);
    }
}

Hooks.on("getSceneControlButtons", (controls) => {
    if (!game.user.isGM) return;

    let jenneSuite;
    const isArray = Array.isArray(controls);
    if (isArray) {
        jenneSuite = controls.find(c => c.name === "jenne-suite");
    } else {
        jenneSuite = controls["jenne-suite"];
    }

    if (!jenneSuite) {
        jenneSuite = {
            name: "jenne-suite",
            title: "Jenne Suite",
            icon: "fa-solid fa-j",
            layer: "jenneSuite",
            visible: true,
            tools: isArray ? [] : {}
        };
        if (isArray) {
            controls.push(jenneSuite);
        } else {
            controls["jenne-suite"] = jenneSuite;
        }
        console.log("Jenne Table Forge | Initialized 'jenne-suite' control group fallback");
    }

    const importerTool = {
        name: "jenne-table-forge",
        title: "TableForge Importer",
        icon: "fas fa-file-pdf",
        button: true,
        visible: true,
        onChange: () => {
            new TableForgeImporterDialog().render(true);
        }
    };

    const isToolsArray = Array.isArray(jenneSuite.tools);
    if (isToolsArray) {
        if (!jenneSuite.tools.some(t => t.name === "jenne-table-forge")) {
            jenneSuite.tools.push(importerTool);
        }
    } else {
        jenneSuite.tools["jenne-table-forge"] = importerTool;
    }
});
