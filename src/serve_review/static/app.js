(function () {
    'use strict';

    // --- State ---
    let mode = 'unknown';                // 'daemon' | 'standalone' | 'unknown'
    let reviews = new Map();             // id -> { summary, fullData }
    let activeReviewId = null;           // null when no review is active
    let eventSource = null;
    let commentsByReview = new Map();    // reviewId -> Map(key -> comment)
    let overallByReview = new Map();     // reviewId -> string

    let reviewData = null;               // active review's full data (read by render fns)
    let comments = new Map();            // active review's comments (read by render fns)
    let activeCommentForm = null;        // {file, line, row} of currently open form
    let selectedCommitSha = null;        // when set, diff is filtered to this commit's files

    // --- Helpers ---

    function esc(str) {
        const d = document.createElement('div');
        d.textContent = str;
        return d.innerHTML;
    }

    function shortSha(sha) {
        return sha.slice(0, 7);
    }

    function branchFromRef(ref) {
        return ref.replace(/^refs\/heads\//, '');
    }

    function fileStats(file) {
        let add = 0, del = 0;
        for (const h of file.hunks) {
            for (const l of h.lines) {
                if (l.line_type === '+') add++;
                else if (l.line_type === '-') del++;
            }
        }
        return { add, del };
    }

    function fileHasFlags(file) {
        return file.hunks.some(h => h.lines.some(l => l.flags && l.flags.length > 0));
    }

    function commentKey(file, line) {
        return file + ':' + line;
    }

    function totalComments() {
        return comments.size;
    }

    // --- Rendering ---

    function render() {
        const app = document.getElementById('app');
        app.innerHTML = '';

        app.appendChild(renderHeader());

        const banner = renderAttentionBanner();
        if (banner) app.appendChild(banner);

        // Layout: sidebar (desktop) + main
        const layout = document.createElement('div');
        layout.id = 'review-layout';

        const sidebar = renderSidebar();
        layout.appendChild(sidebar);

        const main = document.createElement('div');
        main.id = 'main-content';

        // Mobile file nav (hidden on desktop via CSS)
        const mobileNav = renderMobileFileNav();
        main.appendChild(mobileNav);

        // File diffs
        for (const file of reviewData.files) {
            main.appendChild(renderFile(file));
        }

        // Overall comment
        main.appendChild(renderOverallComment());

        layout.appendChild(main);
        app.appendChild(layout);
        app.appendChild(renderActionBar());

        if (mode === 'daemon') {
            renderTabBar();
        }

        // Measure sticky elements and set CSS variable for sticky file
        // headers. Only count what stays in the viewport when scrolled: the
        // tab bar (sticky, top: 0) and the review header (sticky, top:
        // var(--tab-bar-h)). The attention banner is intentionally NOT
        // sticky, so leaving room for it would leave a gap once scrolled
        // past.
        requestAnimationFrame(function () {
            var tabBar = document.getElementById('tab-bar');
            var tabBarH = tabBar ? tabBar.offsetHeight : 0;
            document.documentElement.style.setProperty('--tab-bar-h', tabBarH + 'px');
            var hdr = document.getElementById('review-header');
            if (hdr) {
                var h = hdr.offsetHeight + tabBarH;
                document.documentElement.style.setProperty('--header-h', h + 'px');
            }
        });
    }

    function renderHeader() {
        const header = document.createElement('header');
        header.id = 'review-header';

        const pi = reviewData.push_info;
        const branch = branchFromRef(pi.local_ref);

        const row = document.createElement('div');
        row.className = 'header-row';

        const name = document.createElement('span');
        name.className = 'branch-name';
        name.textContent = branch;
        row.appendChild(name);

        if (pi.is_force_push) {
            const badge = document.createElement('span');
            badge.className = 'badge badge-force';
            badge.textContent = 'force push';
            row.appendChild(badge);
        }

        if (pi.remote_sha === '0'.repeat(40)) {
            const badge = document.createElement('span');
            badge.className = 'badge badge-new-branch';
            badge.textContent = 'new branch';
            row.appendChild(badge);
        }

        header.appendChild(row);

        const stats = document.createElement('div');
        stats.className = 'header-stats';
        stats.innerHTML =
            '<span class="header-stat">' + reviewData.commits.length + ' commit' +
            (reviewData.commits.length !== 1 ? 's' : '') + '</span>' +
            '<span class="header-stat">' + reviewData.files.length + ' file' +
            (reviewData.files.length !== 1 ? 's' : '') + '</span>';
        if (pi.remote_name) {
            stats.innerHTML += '<span class="header-stat">&rarr; ' + esc(pi.remote_name) + '</span>';
        }
        header.appendChild(stats);

        return header;
    }

    function renderAttentionBanner() {
        if (!reviewData.has_attention_flags) return null;

        const banner = document.createElement('div');
        banner.id = 'attention-banner';

        const text = document.createElement('span');
        text.textContent = '\u26A0 Items flagged for attention';
        banner.appendChild(text);

        const btn = document.createElement('button');
        btn.textContent = 'Jump to first';
        btn.addEventListener('click', jumpToFirstFlag);
        banner.appendChild(btn);

        return banner;
    }

    function renderSidebar() {
        const sidebar = document.createElement('aside');
        sidebar.id = 'sidebar';

        // Commits section
        sidebar.appendChild(renderCommitsSection());

        // Files section
        const filesSection = document.createElement('div');
        filesSection.className = 'section open';

        const filesToggle = document.createElement('button');
        filesToggle.className = 'section-toggle';
        filesToggle.textContent = 'Files (' + reviewData.files.length + ')';
        filesToggle.addEventListener('click', () => toggleSection(filesSection));
        filesSection.appendChild(filesToggle);

        const wrap = document.createElement('div');
        wrap.className = 'section-body-wrap';
        const body = document.createElement('div');
        body.className = 'section-body';

        const list = document.createElement('ul');
        list.className = 'file-nav-list';

        for (const file of reviewData.files) {
            const item = document.createElement('li');
            item.className = 'file-nav-item';
            item.addEventListener('click', () => scrollToFile(file.new_path));

            if (fileHasFlags(file)) {
                const dot = document.createElement('span');
                dot.className = 'file-nav-flag';
                item.appendChild(dot);
            }

            const nameWrap = document.createElement('span');
            nameWrap.className = 'file-nav-name';
            const nameInner = document.createElement('span');
            nameInner.textContent = file.new_path;
            nameWrap.appendChild(nameInner);
            item.appendChild(nameWrap);

            const s = fileStats(file);
            const statsSpan = document.createElement('span');
            statsSpan.className = 'file-nav-stats';
            statsSpan.innerHTML =
                '<span class="add">+' + s.add + '</span> ' +
                '<span class="del">-' + s.del + '</span>';
            item.appendChild(statsSpan);

            list.appendChild(item);
        }

        body.appendChild(list);
        wrap.appendChild(body);
        filesSection.appendChild(wrap);
        sidebar.appendChild(filesSection);

        return sidebar;
    }

    function renderMobileFileNav() {
        const section = document.createElement('div');
        section.id = 'mobile-file-nav';
        section.className = 'section';

        const toggle = document.createElement('button');
        toggle.className = 'section-toggle';
        toggle.textContent = 'Files (' + reviewData.files.length + ')';
        toggle.addEventListener('click', () => toggleSection(section));
        section.appendChild(toggle);

        const wrap = document.createElement('div');
        wrap.className = 'section-body-wrap';
        const body = document.createElement('div');
        body.className = 'section-body';

        const list = document.createElement('ul');
        list.className = 'file-nav-list';

        for (const file of reviewData.files) {
            const item = document.createElement('li');
            item.className = 'file-nav-item';
            item.addEventListener('click', () => {
                toggleSection(section); // close nav
                scrollToFile(file.new_path);
            });

            if (fileHasFlags(file)) {
                const dot = document.createElement('span');
                dot.className = 'file-nav-flag';
                item.appendChild(dot);
            }

            const nameWrap = document.createElement('span');
            nameWrap.className = 'file-nav-name';
            const nameInner = document.createElement('span');
            nameInner.textContent = file.new_path;
            nameWrap.appendChild(nameInner);
            item.appendChild(nameWrap);

            const s = fileStats(file);
            const statsSpan = document.createElement('span');
            statsSpan.className = 'file-nav-stats';
            statsSpan.innerHTML =
                '<span class="add">+' + s.add + '</span> ' +
                '<span class="del">-' + s.del + '</span>';
            item.appendChild(statsSpan);

            list.appendChild(item);
        }

        body.appendChild(list);
        wrap.appendChild(body);
        section.appendChild(wrap);

        // Wrap with commits in a container that's hidden on desktop (sidebar handles it there)
        const commitsSection = renderCommitsSection();
        const container = document.createElement('div');
        container.id = 'mobile-nav-container';
        container.appendChild(commitsSection);
        container.appendChild(section);
        return container;
    }

    function renderCommitsSection() {
        const section = document.createElement('div');
        section.className = 'section open';

        const toggle = document.createElement('button');
        toggle.className = 'section-toggle';
        toggle.textContent = 'Commits (' + reviewData.commits.length + ')';
        toggle.addEventListener('click', () => toggleSection(section));
        section.appendChild(toggle);

        const wrap = document.createElement('div');
        wrap.className = 'section-body-wrap';
        const body = document.createElement('div');
        body.className = 'section-body';

        const list = document.createElement('ul');
        list.className = 'commit-list';
        list.id = 'commit-list';

        for (const commit of reviewData.commits) {
            const item = document.createElement('li');
            item.className = 'commit-item';
            item.dataset.sha = commit.sha;

            const header = document.createElement('div');
            header.className = 'commit-header';
            header.addEventListener('click', function () {
                selectCommit(commit.sha);
            });

            const sha = document.createElement('span');
            sha.className = 'commit-sha';
            sha.textContent = shortSha(commit.sha);

            const msg = document.createElement('span');
            msg.className = 'commit-message';
            msg.textContent = commit.message;

            const meta = document.createElement('span');
            meta.className = 'commit-meta';
            meta.textContent = commit.author;

            header.appendChild(sha);
            header.appendChild(msg);
            header.appendChild(meta);
            item.appendChild(header);

            // Expandable body (full message + file count)
            if (commit.body || commit.files.length > 0) {
                const detail = document.createElement('div');
                detail.className = 'commit-detail';

                if (commit.body) {
                    const bodyEl = document.createElement('pre');
                    bodyEl.className = 'commit-body';
                    bodyEl.textContent = commit.body;
                    detail.appendChild(bodyEl);
                }

                if (commit.files.length > 0) {
                    const fileCount = document.createElement('span');
                    fileCount.className = 'commit-file-count';
                    fileCount.textContent = commit.files.length + ' file' +
                        (commit.files.length !== 1 ? 's' : '') + ' changed';
                    detail.appendChild(fileCount);
                }

                item.appendChild(detail);
            }

            list.appendChild(item);
        }

        // "Show all" button (visible when a commit is selected)
        const showAll = document.createElement('button');
        showAll.className = 'commit-show-all hidden';
        showAll.id = 'commit-show-all';
        showAll.textContent = 'Show all commits';
        showAll.addEventListener('click', function () {
            selectCommit(null);
        });
        list.appendChild(showAll);

        body.appendChild(list);
        wrap.appendChild(body);
        section.appendChild(wrap);

        return section;
    }

    function renderFile(file) {
        const div = document.createElement('div');
        div.className = 'file-diff open';
        div.dataset.file = file.new_path;

        // Header
        const header = document.createElement('div');
        header.className = 'file-header';
        header.addEventListener('click', () => div.classList.toggle('open'));

        const arrow = document.createElement('span');
        arrow.className = 'file-toggle-arrow';
        arrow.textContent = '\u25B6';
        header.appendChild(arrow);

        const nameEl = document.createElement('span');
        nameEl.className = 'file-name';
        if (fileHasFlags(file)) {
            const dot = document.createElement('span');
            dot.className = 'flag-dot';
            nameEl.appendChild(dot);
        }
        nameEl.appendChild(document.createTextNode(file.new_path));
        header.appendChild(nameEl);

        if (file.is_new) {
            const badge = document.createElement('span');
            badge.className = 'file-badge file-badge-new';
            badge.textContent = 'new';
            header.appendChild(badge);
        } else if (file.is_deleted) {
            const badge = document.createElement('span');
            badge.className = 'file-badge file-badge-deleted';
            badge.textContent = 'deleted';
            header.appendChild(badge);
        } else if (file.is_rename) {
            const badge = document.createElement('span');
            badge.className = 'file-badge file-badge-renamed';
            badge.textContent = 'renamed';
            header.appendChild(badge);
        }

        const s = fileStats(file);
        const stats = document.createElement('span');
        stats.className = 'file-stats';
        stats.innerHTML =
            '<span class="add">+' + s.add + '</span> ' +
            '<span class="del">-' + s.del + '</span>';
        header.appendChild(stats);

        div.appendChild(header);

        // Body
        const bodyWrap = document.createElement('div');
        bodyWrap.className = 'file-body-wrap';
        const body = document.createElement('div');
        body.className = 'file-body';
        const inner = document.createElement('div');
        inner.className = 'file-body-inner';

        const table = document.createElement('table');
        table.className = 'diff-table';

        for (const hunk of file.hunks) {
            // Hunk header row
            const hunkRow = document.createElement('tr');
            hunkRow.className = 'hunk-header';
            const hunkCell = document.createElement('td');
            hunkCell.colSpan = 4;
            hunkCell.textContent = hunk.header;
            hunkRow.appendChild(hunkCell);
            table.appendChild(hunkRow);

            // Diff lines
            for (const line of hunk.lines) {
                table.appendChild(renderDiffLine(line, file));

                // Show existing comment if any
                const key = commentKey(file.new_path, line.new_line_no || line.old_line_no);
                if (comments.has(key)) {
                    table.appendChild(renderExistingComment(key));
                }
            }
        }

        inner.appendChild(table);
        body.appendChild(inner);
        bodyWrap.appendChild(body);
        div.appendChild(bodyWrap);

        return div;
    }

    function renderDiffLine(line, file) {
        const tr = document.createElement('tr');
        tr.className = 'diff-line';

        if (line.line_type === '+') tr.classList.add('line-added');
        else if (line.line_type === '-') tr.classList.add('line-removed');

        const hasFlags = line.flags && line.flags.length > 0;
        if (hasFlags) tr.classList.add('has-flags');

        const lineNo = line.new_line_no || line.old_line_no;
        tr.dataset.file = file.new_path;
        tr.dataset.line = lineNo || '';

        // Check if this line has a comment
        const key = commentKey(file.new_path, lineNo);
        const hasComment = comments.has(key);

        // Old line number
        const oldTd = document.createElement('td');
        oldTd.className = 'line-no';
        if (line.old_line_no != null) {
            oldTd.textContent = line.old_line_no;
        }
        tr.appendChild(oldTd);

        // New line number
        const newTd = document.createElement('td');
        newTd.className = 'line-no';
        if (line.new_line_no != null) {
            newTd.textContent = line.new_line_no;
            if (hasComment) {
                const badge = document.createElement('span');
                badge.className = 'line-comment-badge';
                newTd.appendChild(badge);
            }
        }
        tr.appendChild(newTd);

        // Prefix
        const prefixTd = document.createElement('td');
        prefixTd.className = 'line-prefix';
        prefixTd.textContent = line.line_type === ' ' ? '\u00A0' : line.line_type;
        tr.appendChild(prefixTd);

        // Content
        const contentTd = document.createElement('td');
        contentTd.className = 'line-content';

        if (hasFlags) {
            contentTd.innerHTML = renderFlaggedContent(line.content, line.flags, file.language);
        } else {
            contentTd.innerHTML = highlightCode(line.content, file.language);
        }

        tr.appendChild(contentTd);

        // Click handler for comments
        tr.addEventListener('click', function (e) {
            if (e.target.closest('.comment-form') || e.target.closest('.existing-comment')) return;
            // Don't open the form if the user just finished selecting text.
            if (window.getSelection && window.getSelection().toString()) return;
            openCommentForm(file.new_path, lineNo, tr);
        });

        return tr;
    }

    function renderFlaggedContent(content, flags, language) {
        if (!flags || flags.length === 0) return highlightCode(content, language);

        const sorted = [...flags].sort((a, b) => a.start - b.start);
        let result = '';
        let pos = 0;

        for (const flag of sorted) {
            const start = Math.max(pos, flag.start);
            const end = flag.end;
            if (start >= end) continue;

            // Prism-highlight the unflagged segment before the flag.
            result += highlightCode(content.slice(pos, start), language);
            // The flagged segment stays plain (amber background draws the
            // eye; syntax colour inside would compete with the highlight).
            result += '<mark class="attention-flag" data-kind="' + esc(flag.kind) + '">';
            result += esc(content.slice(start, end));
            result += '</mark>';
            pos = end;
        }
        result += highlightCode(content.slice(pos), language);
        return result;
    }

    function highlightCode(content, language) {
        if (typeof Prism !== 'undefined' && Prism.languages[language]) {
            return Prism.highlight(content, Prism.languages[language], language);
        }
        return esc(content);
    }

    function renderExistingComment(key) {
        const comment = comments.get(key);
        const tr = document.createElement('tr');
        tr.className = 'comment-row';
        tr.dataset.commentKey = key;

        const td = document.createElement('td');
        td.colSpan = 4;

        const div = document.createElement('div');
        div.className = 'existing-comment';

        const body = document.createElement('span');
        body.className = 'comment-body';
        body.textContent = comment.body;

        const actions = document.createElement('span');
        actions.className = 'comment-actions';

        const editBtn = document.createElement('button');
        editBtn.textContent = 'edit';
        editBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            const prevRow = tr.previousElementSibling;
            comments.delete(key);
            tr.remove();
            if (prevRow) {
                openCommentForm(comment.file, comment.line, prevRow, comment.body);
            }
        });

        const delBtn = document.createElement('button');
        delBtn.textContent = 'delete';
        delBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            comments.delete(key);
            tr.remove();
            // Remove badge from line
            const lineRow = document.querySelector(
                '.diff-line[data-file="' + CSS.escape(comment.file) + '"][data-line="' + comment.line + '"]'
            );
            if (lineRow) {
                const badge = lineRow.querySelector('.line-comment-badge');
                if (badge) badge.remove();
            }
            updateCommentCount();
        });

        actions.appendChild(editBtn);
        actions.appendChild(delBtn);

        div.appendChild(body);
        div.appendChild(actions);
        td.appendChild(div);
        tr.appendChild(td);

        return tr;
    }

    function renderOverallComment() {
        const section = document.createElement('div');
        section.id = 'overall-section';

        const label = document.createElement('label');
        label.htmlFor = 'overall-comment';
        label.textContent = 'Overall comment';
        section.appendChild(label);

        const textarea = document.createElement('textarea');
        textarea.id = 'overall-comment';
        textarea.placeholder = 'Optional overall comment...';
        section.appendChild(textarea);

        return section;
    }

    function renderActionBar() {
        const bar = document.createElement('div');
        bar.id = 'action-bar';

        const count = document.createElement('span');
        count.className = 'comment-count';
        count.id = 'comment-count';
        updateCommentCountEl(count);
        bar.appendChild(count);

        const spacer = document.createElement('span');
        spacer.className = 'action-spacer';
        bar.appendChild(spacer);

        const approveBtn = document.createElement('button');
        approveBtn.className = 'btn-approve';
        approveBtn.textContent = 'Approve';
        approveBtn.addEventListener('click', handleApprove);
        bar.appendChild(approveBtn);

        const denyBtn = document.createElement('button');
        denyBtn.className = 'btn-deny';
        denyBtn.textContent = 'Request Changes';
        denyBtn.addEventListener('click', handleDeny);
        bar.appendChild(denyBtn);

        return bar;
    }

    // --- Comment system ---

    function openCommentForm(filePath, lineNo, afterRow, prefill) {
        if (!lineNo) return;

        // Close any existing form
        closeCommentForm();

        const tr = document.createElement('tr');
        tr.className = 'comment-form-row';

        const td = document.createElement('td');
        td.colSpan = 4;

        const form = document.createElement('div');
        form.className = 'comment-form';

        const textarea = document.createElement('textarea');
        textarea.placeholder = 'Leave a comment on this line...';
        if (prefill) textarea.value = prefill;

        const actions = document.createElement('div');
        actions.className = 'comment-form-actions';

        const cancelBtn = document.createElement('button');
        cancelBtn.className = 'btn-cancel';
        cancelBtn.textContent = 'Cancel';
        cancelBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            closeCommentForm();
        });

        const saveBtn = document.createElement('button');
        saveBtn.className = 'btn-save';
        saveBtn.textContent = 'Save';
        saveBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            const body = textarea.value.trim();
            if (!body) return;

            const key = commentKey(filePath, lineNo);
            comments.set(key, { body: body, file: filePath, line: lineNo });

            closeCommentForm();

            // Add badge to the line if not present
            const lineRow = afterRow;
            if (lineRow && !lineRow.querySelector('.line-comment-badge')) {
                const newNoTd = lineRow.querySelectorAll('.line-no')[1];
                if (newNoTd) {
                    const badge = document.createElement('span');
                    badge.className = 'line-comment-badge';
                    newNoTd.appendChild(badge);
                }
            }

            // Insert comment display row
            const commentRow = renderExistingComment(key);
            afterRow.parentNode.insertBefore(commentRow, afterRow.nextSibling);

            updateCommentCount();
        });

        actions.appendChild(cancelBtn);
        actions.appendChild(saveBtn);

        form.appendChild(textarea);
        form.appendChild(actions);
        td.appendChild(form);
        tr.appendChild(td);

        // Insert after the clicked row
        afterRow.parentNode.insertBefore(tr, afterRow.nextSibling);
        textarea.focus();

        activeCommentForm = { file: filePath, line: lineNo, row: tr };
    }

    function closeCommentForm() {
        if (activeCommentForm) {
            activeCommentForm.row.remove();
            activeCommentForm = null;
        }
    }

    function updateCommentCount() {
        const el = document.getElementById('comment-count');
        if (el) updateCommentCountEl(el);
    }

    function updateCommentCountEl(el) {
        const n = totalComments();
        if (n === 0) {
            el.textContent = '';
        } else {
            el.innerHTML = '<strong>' + n + '</strong> comment' + (n !== 1 ? 's' : '');
        }
    }

    // --- Navigation ---

    function scrollToFile(filePath) {
        const el = document.querySelector('.file-diff[data-file="' + CSS.escape(filePath) + '"]');
        if (el) {
            if (!el.classList.contains('open')) {
                el.classList.add('open');
            }
            el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
    }

    function jumpToFirstFlag() {
        const el = document.querySelector('.diff-line.has-flags');
        if (el) {
            // Ensure parent file is open
            const fileDiv = el.closest('.file-diff');
            if (fileDiv && !fileDiv.classList.contains('open')) {
                fileDiv.classList.add('open');
            }
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            // Brief highlight
            el.style.outline = '2px solid var(--amber)';
            setTimeout(() => { el.style.outline = ''; }, 2000);
        }
    }

    function toggleSection(section) {
        section.classList.toggle('open');
    }

    function selectCommit(sha) {
        // Toggle: clicking the same commit deselects
        if (selectedCommitSha === sha) sha = null;
        selectedCommitSha = sha;

        // Update commit list highlighting
        document.querySelectorAll('.commit-item').forEach(function (item) {
            if (sha && item.dataset.sha === sha) {
                item.classList.add('selected');
            } else {
                item.classList.remove('selected');
            }
        });

        // Show/hide "Show all" button
        var showAllBtn = document.getElementById('commit-show-all');
        if (showAllBtn) {
            showAllBtn.classList.toggle('hidden', !sha);
        }

        // Get file list for the selected commit
        var commitFiles = null;
        if (sha) {
            var commit = reviewData.commits.find(function (c) { return c.sha === sha; });
            if (commit) commitFiles = new Set(commit.files);
        }

        // git_ops produces both diff paths (via _strip_diff_prefix) and
        // commit file lists (via git log --name-only) without a/b prefixes,
        // so a single membership check is sufficient.
        document.querySelectorAll('.file-diff').forEach(function (el) {
            if (!commitFiles) {
                el.classList.remove('filtered-out');
            } else {
                el.classList.toggle('filtered-out', !commitFiles.has(el.dataset.file));
            }
        });

        document.querySelectorAll('.file-nav-item').forEach(function (item) {
            if (!commitFiles) {
                item.classList.remove('filtered-out');
            } else {
                var nameEl = item.querySelector('.file-nav-name span');
                if (nameEl) {
                    item.classList.toggle('filtered-out', !commitFiles.has(nameEl.textContent));
                }
            }
        });

        // Update header stats to show filter
        var headerStats = document.querySelector('.header-stats');
        if (headerStats) {
            var existing = document.getElementById('commit-filter-indicator');
            if (existing) existing.remove();
            if (sha) {
                var indicator = document.createElement('span');
                indicator.id = 'commit-filter-indicator';
                indicator.className = 'header-stat commit-filter-active';
                indicator.textContent = 'filtered to ' + sha.slice(0, 7);
                headerStats.appendChild(indicator);
            }
        }
    }

    // --- Actions ---

    function approveUrl() {
        return mode === 'daemon'
            ? '/api/queue/' + activeReviewId + '/approve'
            : '/api/review/approve';
    }

    function denyUrl() {
        return mode === 'daemon'
            ? '/api/queue/' + activeReviewId + '/deny'
            : '/api/review/deny';
    }

    async function handleApprove() {
        const overall = document.getElementById('overall-comment');
        const overallComment = overall ? overall.value.trim() : '';

        try {
            const res = await fetch(approveUrl(), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ overall_comment: overallComment }),
            });
            if (res.ok) {
                if (mode === 'standalone') {
                    showSubmitted('approved');
                }
                // In daemon mode, the SSE review_decided event handler triggers
                // the tab styling. The user's view stays put; the tab fades and
                // is removed by SSE events.
            } else {
                const data = await res.json();
                alert('Error: ' + (data.error || 'Unknown error'));
            }
        } catch (err) {
            alert('Network error: ' + err.message);
        }
    }

    async function handleDeny() {
        const overall = document.getElementById('overall-comment');
        const overallComment = overall ? overall.value.trim() : '';

        const commentList = [];
        for (const c of comments.values()) {
            commentList.push({ body: c.body, file: c.file, line: c.line });
        }

        if (commentList.length === 0 && !overallComment) {
            // Prompt for at least some feedback
            const overall2 = document.getElementById('overall-comment');
            if (overall2) {
                overall2.focus();
                overall2.placeholder = 'Please explain what needs to change...';
                overall2.style.borderColor = 'var(--red)';
                setTimeout(() => { overall2.style.borderColor = ''; }, 3000);
            }
            return;
        }

        try {
            const res = await fetch(denyUrl(), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    overall_comment: overallComment,
                    comments: commentList,
                }),
            });
            if (res.ok) {
                if (mode === 'standalone') {
                    showSubmitted('denied');
                }
                // In daemon mode, the SSE review_decided event handler triggers
                // the tab styling.
            } else {
                const data = await res.json();
                alert('Error: ' + (data.error || 'Unknown error'));
            }
        } catch (err) {
            alert('Network error: ' + err.message);
        }
    }

    function showSubmitted(decision) {
        const overlay = document.createElement('div');
        overlay.id = 'submitted-overlay';

        const icon = document.createElement('div');
        icon.className = 'submitted-icon ' + decision;
        icon.textContent = decision === 'approved' ? '\u2713' : '\u2717';

        const title = document.createElement('div');
        title.className = 'submitted-title';
        title.textContent = decision === 'approved' ? 'Approved' : 'Changes Requested';

        const subtitle = document.createElement('div');
        subtitle.className = 'submitted-subtitle';
        subtitle.textContent = 'You can close this tab. The terminal will continue.';

        overlay.appendChild(icon);
        overlay.appendChild(title);
        overlay.appendChild(subtitle);

        document.body.appendChild(overlay);
    }

    // --- Keyboard shortcuts ---

    document.addEventListener('keydown', function (e) {
        // Don't capture when typing in inputs
        if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;

        if (e.key === 'n' || e.key === 'N') {
            jumpToFirstFlag();
        }
    });

    // --- PWA ---

    function registerSW() {
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/static/sw.js').catch(function () {
                // SW registration failed, not critical
            });
        }
    }

    // --- Tab bar ---

    function renderTabBar() {
        let bar = document.getElementById('tab-bar');
        if (mode !== 'daemon') {
            if (bar) bar.remove();
            return;
        }
        if (!bar) {
            bar = document.createElement('div');
            bar.id = 'tab-bar';
            document.body.insertBefore(bar, document.getElementById('app'));
        }
        bar.innerHTML = '';
        // Sort by submitted_at ascending so order is stable
        const sorted = Array.from(reviews.values()).sort(
            (a, b) => a.summary.submitted_at - b.summary.submitted_at
        );
        for (const review of sorted) {
            const tab = document.createElement('button');
            tab.className = 'review-tab';
            if (review.summary.id === activeReviewId) tab.classList.add('active');
            if (review.summary.status === 'decided') {
                tab.classList.add('decided');
                // Differentiate approve vs deny so the tab flashes the
                // matching colour during the 3s fade. The decision dict is
                // stashed on the summary by the review_decided SSE handler.
                const verdict = review.summary.decision &&
                    review.summary.decision.decision;
                if (verdict === 'deny') tab.classList.add('decided-deny');
                else tab.classList.add('decided-approve');
            }
            if (review.summary.status === 'orphaned') tab.classList.add('orphaned');
            const branch = review.summary.branch;
            const fileCount = review.summary.files_count;
            tab.textContent = branch + ' (' + fileCount + ')';
            tab.dataset.reviewId = review.summary.id;
            tab.addEventListener('click', () => switchTab(review.summary.id));

            if (review.summary.status === 'pending') {
                const dismiss = document.createElement('button');
                dismiss.className = 'tab-dismiss';
                dismiss.textContent = '×';
                dismiss.title = 'Dismiss this review';
                dismiss.addEventListener('click', async function (e) {
                    e.stopPropagation();
                    await fetch('/api/queue/' + review.summary.id + '/cancel', { method: 'POST' });
                });
                tab.appendChild(dismiss);
            }

            bar.appendChild(tab);
        }
    }

    async function switchTab(reviewId) {
        // Save the outgoing tab's comments and overall-text into per-review state
        if (activeReviewId !== null) {
            commentsByReview.set(activeReviewId, comments);
            const overallEl = document.getElementById('overall-comment');
            if (overallEl) overallByReview.set(activeReviewId, overallEl.value);
        }

        activeReviewId = reviewId;
        const review = reviews.get(reviewId);
        if (!review) {
            renderEmptyState();
            return;
        }
        if (!review.fullData) {
            const resp = await fetch('/api/queue/' + reviewId);
            if (!resp.ok) {
                // Review was reaped between summary and tab click
                reviews.delete(reviewId);
                activeReviewId = null;
                renderTabBar();
                const next = reviews.keys().next().value;
                if (next) {
                    await switchTab(next);
                } else {
                    renderEmptyState();
                }
                return;
            }
            review.fullData = await resp.json();
        }
        reviewData = review.fullData;
        comments = commentsByReview.get(reviewId) || new Map();
        render();
        // Restore the overall-comment textarea after render
        const overallEl = document.getElementById('overall-comment');
        if (overallEl) overallEl.value = overallByReview.get(reviewId) || '';
    }

    function renderEmptyState() {
        const app = document.getElementById('app');
        app.innerHTML = '';
        if (mode === 'daemon') renderTabBar();
        const empty = document.createElement('div');
        empty.id = 'empty-state';
        empty.textContent = 'Waiting for reviews...';
        app.appendChild(empty);
    }

    // --- SSE ---

    function connectSSE() {
        if (eventSource) return;
        eventSource = new EventSource('/api/events');

        eventSource.addEventListener('review_added', async function (e) {
            const data = JSON.parse(e.data);
            reviews.set(data.id, { summary: data.summary, fullData: null });
            renderTabBar();
            // If nothing is active, switch to the new one
            if (activeReviewId === null) {
                await switchTab(data.id);
            }
        });

        eventSource.addEventListener('review_decided', function (e) {
            const data = JSON.parse(e.data);
            const review = reviews.get(data.id);
            if (review) {
                review.summary.status = 'decided';
                review.summary.decision = data.decision;
                renderTabBar();
            }
        });

        eventSource.addEventListener('review_orphaned', function (e) {
            const data = JSON.parse(e.data);
            const review = reviews.get(data.id);
            if (review) {
                review.summary.status = 'orphaned';
                renderTabBar();
            }
        });

        eventSource.addEventListener('review_removed', function (e) {
            const data = JSON.parse(e.data);
            const wasActive = data.id === activeReviewId;
            reviews.delete(data.id);
            commentsByReview.delete(data.id);
            overallByReview.delete(data.id);
            if (wasActive) {
                activeReviewId = null;
                const next = reviews.keys().next().value;
                if (next) {
                    switchTab(next);
                } else {
                    renderEmptyState();
                }
            } else {
                renderTabBar();
            }
        });

        eventSource.onerror = function () {
            // Auto-reconnects by default; if it gives up, recreate after backoff.
            if (eventSource && eventSource.readyState === EventSource.CLOSED) {
                eventSource = null;
                setTimeout(connectSSE, 3000);
            }
        };
    }

    // --- Init ---

    async function init() {
        try {
            const queueResp = await fetch('/api/queue');
            if (queueResp.ok) {
                mode = 'daemon';
                const summaries = await queueResp.json();
                for (const s of summaries) {
                    reviews.set(s.id, { summary: s, fullData: null });
                }
                connectSSE();
                if (summaries.length > 0) {
                    await switchTab(summaries[0].id);
                } else {
                    renderEmptyState();
                }
            } else {
                await initStandalone();
            }
        } catch (err) {
            // Network error: try standalone fallback
            await initStandalone();
        }
        registerSW();
    }

    async function initStandalone() {
        mode = 'standalone';
        try {
            const res = await fetch('/api/review');
            if (!res.ok) throw new Error('HTTP ' + res.status);
            reviewData = await res.json();
            comments = new Map();
            render();
        } catch (err) {
            document.getElementById('app').innerHTML =
                '<div id="loading"><div class="loading-text">Failed to load review data: ' +
                esc(err.message) + '</div></div>';
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
