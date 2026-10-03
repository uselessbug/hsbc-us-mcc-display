// ==UserScript==
// @name         HSBC US Credit Card MCC Display
// @namespace    https://github.com/uselessbug/hsbc-us-mcc-display
// @version      4.3.2
// @description  Show posted Mastercard MCCs and locally predict pending MCCs from merchant history.
// @homepageURL  https://github.com/uselessbug/hsbc-us-mcc-display
// @supportURL   https://github.com/uselessbug/hsbc-us-mcc-display/issues
// @downloadURL  https://raw.githubusercontent.com/uselessbug/hsbc-us-mcc-display/main/hsbc-mcc-display.user.js
// @updateURL    https://raw.githubusercontent.com/uselessbug/hsbc-us-mcc-display/main/hsbc-mcc-display.user.js
// @match        https://onlinebanking.firstdata.com/*
// @run-at       document-start
// @grant        unsafeWindow
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @connect      raw.githubusercontent.com
// @noframes
// ==/UserScript==

(() => {
    'use strict';

    const DB_URL = 'https://raw.githubusercontent.com/uselessbug/hsbc-us-mcc-display/main/data/mcc-mastercard.json';
    const DB_SCHEMA = 1;
    const HISTORY_SCHEMA = 1;
    const HISTORY_MAX_MERCHANTS = 1200;
    const REFRESH_MS = 7 * 24 * 60 * 60 * 1000;
    const RETRY_MS = 6 * 60 * 60 * 1000;
    const STORE = Object.freeze({
        db: 'hsbcMcc.mastercardDb',
        etag: 'hsbcMcc.mastercardDb.etag',
        modified: 'hsbcMcc.mastercardDb.lastModified',
        success: 'hsbcMcc.mastercardDb.lastSuccess',
        attempt: 'hsbcMcc.mastercardDb.lastAttempt',
        history: 'hsbcMcc.merchantHistory',
    });

    // FirstData exposes ISO 4217 numeric codes. Keep the common HSBC US travel
    // currencies compact; fall back to FirstData's own currency-name record.
    const CURRENCY_ALPHA = Object.freeze({
        '036': 'AUD',
        '124': 'CAD',
        '156': 'CNY',
        '344': 'HKD',
        '392': 'JPY',
        '410': 'KRW',
        '446': 'MOP',
        '702': 'SGD',
        '764': 'THB',
        '826': 'GBP',
        '840': 'USD',
        '901': 'TWD',
        '978': 'EUR',
    });

    const posted = new Map();
    let postedExport = [];
    let merchantHistory = emptyHistory();
    let mccDb = Object.create(null);
    let mccMeta = null;
    let drawPending = false;
    let tooltip = null;
    let latestRequest = 0;

    const log = (...args) => console.debug('[HSBC MCC]', ...args);

    function normDesc(value) {
        return String(value ?? '').normalize('NFKC').replace(/[^\p{L}\p{N}]/gu, '').toUpperCase();
    }

    function merchantKey(value) {
        return String(value ?? '')
            .normalize('NFKC')
            .toUpperCase()
            .replace(/[^\p{L}\p{N}]+/gu, ' ')
            .replace(/\s+/g, ' ')
            .trim();
    }

    function emptyHistory() {
        return {
            schemaVersion: HISTORY_SCHEMA,
            merchants: Object.create(null),
            seen: Object.create(null),
        };
    }

    function validHistory(history) {
        return Boolean(
            history
            && history.schemaVersion === HISTORY_SCHEMA
            && history.merchants
            && typeof history.merchants === 'object'
            && !Array.isArray(history.merchants)
            && history.seen
            && typeof history.seen === 'object'
            && !Array.isArray(history.seen)
        );
    }

    function loadMerchantHistory() {
        const raw = GM_getValue(STORE.history, '');
        if (!raw) return;
        try {
            const history = JSON.parse(raw);
            if (validHistory(history)) merchantHistory = history;
        } catch (error) {
            log('Ignoring invalid merchant history:', error);
        }
    }

    function saveMerchantHistory() {
        GM_setValue(STORE.history, JSON.stringify(merchantHistory));
    }

    function hashText(value) {
        let hash = 0x811c9dc5;
        for (let i = 0; i < value.length; i += 1) {
            hash ^= value.charCodeAt(i);
            hash = Math.imul(hash, 0x01000193);
        }
        return (hash >>> 0).toString(36);
    }

    function observationKey(tx) {
        const id = String(tx?.transactionId ?? '').trim();
        if (id) return hashText(`id:${id}`);
        const ref = String(tx?.microfilmReferenceNumber ?? '').trim();
        if (ref) return hashText(`ref:${ref}`);
        const key = apiSignature(tx);
        return key ? hashText(`sig:${key}`) : null;
    }

    function pruneMerchantHistory() {
        const merchantEntries = Object.entries(merchantHistory.merchants);
        if (merchantEntries.length > HISTORY_MAX_MERCHANTS) {
            merchantEntries
                .sort((a, b) => Number(b[1]?.lastSeen ?? 0) - Number(a[1]?.lastSeen ?? 0))
                .slice(HISTORY_MAX_MERCHANTS)
                .forEach(([key]) => delete merchantHistory.merchants[key]);
        }
    }

    function learnPostedHistory(list) {
        let changed = false;
        const now = Date.now();

        for (const tx of list) {
            if (!tx?.transactionCode?.display) continue;
            const mcc = normMcc(tx?.merchantCategoryCode);
            const merchant = merchantKey(tx?.description);
            const observation = observationKey(tx);
            if (!mcc || merchant.length < 4 || !observation || merchantHistory.seen[observation]) continue;

            const record = merchantHistory.merchants[merchant] ?? {
                counts: Object.create(null),
                lastSeen: 0,
            };
            if (!record.counts || typeof record.counts !== 'object' || Array.isArray(record.counts)) {
                record.counts = Object.create(null);
            }

            record.counts[mcc] = (Number(record.counts[mcc]) || 0) + 1;
            record.lastSeen = now;
            merchantHistory.merchants[merchant] = record;
            merchantHistory.seen[observation] = now;
            changed = true;
        }

        if (!changed) return;
        pruneMerchantHistory();
        saveMerchantHistory();
        log('Updated merchant MCC history:', Object.keys(merchantHistory.merchants).length, 'merchant descriptions');
    }

    function predictMcc(description) {
        const query = merchantKey(description);
        if (query.length < 4) return null;

        const counts = Object.create(null);
        let matchedMerchants = 0;

        for (const [merchant, record] of Object.entries(merchantHistory.merchants)) {
            const matches = merchant === query || merchant.startsWith(`${query} `);
            if (!matches || !record?.counts) continue;

            matchedMerchants += 1;
            for (const [mcc, rawCount] of Object.entries(record.counts)) {
                const count = Number(rawCount) || 0;
                if (count > 0) counts[mcc] = (counts[mcc] || 0) + count;
            }
        }

        const ranked = Object.entries(counts)
            .filter(([mcc, count]) => normMcc(mcc) && count > 0)
            .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
        if (!ranked.length) return null;

        const total = ranked.reduce((sum, [, count]) => sum + count, 0);
        const [mcc, hits] = ranked[0];
        const share = hits / total;

        // One clean historical match is useful but explicitly shown as low
        // confidence. If several historical MCCs disagree, require a clear
        // dominant result instead of guessing.
        if (total > 1 && share < 0.75) return null;

        const confidence = total >= 5 && share >= 0.9
            ? 'high'
            : total >= 2 && share >= 0.8
                ? 'medium'
                : 'low';

        return {
            mcc,
            hits,
            total,
            share,
            confidence,
            matchedMerchants,
        };
    }

    function normMcc(value) {
        const s = String(value ?? '').trim();
        return /^\d{1,4}$/.test(s) ? s.padStart(4, '0') : null;
    }

    function normAmount(value) {
        const n = typeof value === 'number'
            ? value
            : Number(String(value ?? '').replace(/[^\d.-]/g, ''));
        return Number.isFinite(n) ? n.toFixed(2) : null;
    }

    function normDate(value) {
        const s = String(value ?? '').trim();
        let m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
        if (m) return `${m[2].padStart(2, '0')}/${m[3].padStart(2, '0')}/${m[1]}`;
        m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})/);
        if (m) return `${m[1].padStart(2, '0')}/${m[2].padStart(2, '0')}/${m[3]}`;
        return null;
    }

    function signature(date, amount, type, desc) {
        const d = normDate(date);
        const a = normAmount(amount);
        const t = String(type ?? '').trim().toUpperCase();
        const x = normDesc(desc);
        return d && a !== null && t && x ? [d, a, t, x].join('\u001f') : null;
    }

    function apiSignature(tx) {
        return signature(
            tx?.transactionDate,
            tx?.transactionAmount,
            tx?.transactionCode?.display,
            tx?.description,
        );
    }

    function rowParts(row) {
        const date = row.querySelector('td[data-label="Date"] .tran_date .small')
            ?? row.querySelector('td[data-label="Date"] .small');
        const desc = row.querySelector('td.desc_column');
        const type = row.querySelector('td.type_column');
        const amount = row.querySelector('.trans_amt');
        const amountCell = amount?.closest('td');
        if (!date || !desc || !type || !amount || !amountCell) return null;

        const clone = type.cloneNode(true);
        clone.querySelectorAll('[data-hsbc-mcc-role]').forEach((node) => node.remove());
        return {
            date: date.textContent.trim(),
            desc: desc.textContent.trim(),
            type: clone.textContent.trim(),
            amount: amount.textContent.trim(),
            cell: type,
            amountCell,
        };
    }

    function extractFx(group, visible) {
        const auxiliaries = group.filter((tx) => !tx?.transactionCode?.display);
        if (!auxiliaries.length) return null;

        let originalAmount = null;
        let exchangeRate = null;
        let currencyName = null;

        for (const tx of auxiliaries) {
            const description = String(tx?.description ?? '');
            const rate = description.match(/([+-]?\d[\d,]*(?:\.\d+)?)\s*X\s*([0-9]+(?:\.\d+)?)/i);
            if (rate) {
                originalAmount = rate[1];
                exchangeRate = rate[2];
                continue;
            }

            const merchant = String(tx?.merchantDescription ?? '').replace(/\s+/g, ' ').trim();
            if (merchant && !/^[-+]?\d/.test(merchant)) currencyName = merchant;
        }

        if (!originalAmount) return null;

        const numeric = String(visible?.currencyCode ?? '').trim().padStart(3, '0');
        const currency = CURRENCY_ALPHA[numeric] ?? currencyName ?? numeric;
        if (!currency) return null;

        if (Number(visible?.transactionAmount) < 0 && !originalAmount.startsWith('-')) {
            originalAmount = `-${originalAmount}`;
        }

        return {
            currency,
            currencyCode: numeric,
            currencyName,
            originalAmount,
            exchangeRate,
        };
    }

    function processPosted(payload) {
        const list = Array.isArray(payload?.transactions) ? payload.transactions : [];
        learnPostedHistory(list);
        const next = new Map();
        const groups = new Map();

        for (const tx of list) {
            const id = String(tx?.transactionId ?? '');
            if (!id) continue;
            const group = groups.get(id) ?? [];
            group.push(tx);
            groups.set(id, group);
        }

        const exportRows = [];

        // Preserve the visible API order. For duplicate signatures HSBC's DOM
        // preserves the same within-day order, so draw() can consume each bucket
        // one occurrence at a time.
        for (const tx of list) {
            if (!tx?.transactionCode?.display) continue; // skip FX auxiliary rows

            const id = String(tx?.transactionId ?? '');
            const ref = String(tx?.microfilmReferenceNumber ?? '').trim();
            const group = (id && groups.get(id)) || [tx];
            const fx = extractFx(group, tx);
            const mcc = normMcc(tx?.merchantCategoryCode);

            exportRows.push({
                transactionDate: tx?.transactionDate ?? '',
                postDate: tx?.postDate ?? '',
                ref,
                transactionAmount: tx?.transactionAmount ?? '',
                description: tx?.description ?? '',
                transactionType: tx?.transactionCode?.display ?? '',
                mcc,
                fx,
            });

            const key = apiSignature(tx);
            if (!key || !mcc) continue;

            const bucket = next.get(key) ?? [];
            bucket.push({
                id,
                ref,
                mcc,
                fx,
            });
            next.set(key, bucket);
        }

        posted.clear();
        for (const [key, bucket] of next) posted.set(key, bucket);
        postedExport = exportRows;
        log('Loaded posted transactions:', [...next.values()].reduce((n, b) => n + b.length, 0));
        scheduleDraw();
    }

    function csvCell(value) {
        return `"${String(value ?? '').replace(/"/g, '""')}"`;
    }

    function buildPostedCsv() {
        const rows = [[
            'Transaction Date',
            'Posting Date',
            'Ref#',
            'Amount',
            'Description',
            'Transaction Type',
            'MCC',
            'MCC Description',
            'Original Currency',
            'Original Amount',
            'Exchange Rate',
        ]];

        for (const tx of postedExport) {
            rows.push([
                tx.transactionDate,
                tx.postDate,
                tx.ref,
                tx.transactionAmount,
                tx.description,
                tx.transactionType,
                tx.mcc ?? '',
                tx.mcc ? (mccDb[tx.mcc] ?? '') : '',
                tx.fx?.currency ?? '',
                tx.fx?.originalAmount ?? '',
                tx.fx?.exchangeRate ?? '',
            ]);
        }

        return rows.map((row) => row.map(csvCell).join(',')).join('\r\n');
    }

    function downloadPostedCsv(filename) {
        const blob = new Blob(['\ufeff', buildPostedCsv()], { type: 'text/csv;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = filename || 'Transactions.csv';
        link.style.display = 'none';
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 0);
    }

    function installDownloadHook() {
        document.addEventListener('click', (event) => {
            const link = event.target.closest?.('a.downbtn[download]');
            const filename = link?.download ?? '';
            if (!link || !/^Transactions\.csv$/i.test(filename) || !postedExport.length) return;

            event.preventDefault();
            downloadPostedCsv(filename);
        }, true);
    }

    function parseXhr(xhr) {
        if (xhr.responseType === 'json') return xhr.response;
        if (xhr.responseType === '' || xhr.responseType === 'text') return JSON.parse(xhr.responseText);
        return null;
    }

    function installNetworkHook() {
        const XHR = unsafeWindow.XMLHttpRequest;
        if (!XHR?.prototype || XHR.prototype.__hsbcMccHooked) return;
        Object.defineProperty(XHR.prototype, '__hsbcMccHooked', { value: true });

        const urls = new WeakMap();
        const open = XHR.prototype.open;
        const send = XHR.prototype.send;

        XHR.prototype.open = new Proxy(open, {
            apply(target, self, args) {
                urls.set(self, String(args[1] ?? ''));
                return Reflect.apply(target, self, args);
            },
        });

        XHR.prototype.send = new Proxy(send, {
            apply(target, self, args) {
                const isPosted = /\/transactions\/v1\/postedtransactions(?:[/?#]|$)/i.test(urls.get(self) ?? '');
                if (isPosted) {
                    const id = ++latestRequest;
                    posted.clear();
                    postedExport = [];
                    scheduleDraw();

                    self.addEventListener('load', () => {
                        if (id !== latestRequest || self.status < 200 || self.status >= 300) return;
                        try {
                            const payload = parseXhr(self);
                            if (payload) processPosted(payload);
                        } catch (error) {
                            log('Unable to parse postedtransactions:', error);
                        }
                    }, { once: true });
                }
                return Reflect.apply(target, self, args);
            },
        });
    }

    function removeTags(cell) {
        cell.querySelectorAll('[data-hsbc-mcc-role]').forEach((node) => node.remove());
        delete cell.dataset.hsbcMcc;
        delete cell.dataset.hsbcMccSignature;
        delete cell.dataset.hsbcMccTransactionId;
        delete cell.dataset.hsbcMccRef;
        delete cell.dataset.hsbcMccAmbiguous;
        delete cell.dataset.hsbcMccPredicted;
    }

    function removeFx(cell) {
        cell.querySelectorAll('[data-hsbc-fx-role]').forEach((node) => node.remove());
        delete cell.dataset.hsbcFxTransactionId;
    }

    function titleFor(mcc) {
        const desc = mccDb[mcc];
        return desc ? `${mcc} - ${desc}` : `MCC ${mcc}`;
    }

    function makeTag(mcc, role) {
        const tag = document.createElement('span');
        tag.className = `hsbc-mcc-tag hsbc-mcc-${role}`;
        tag.dataset.hsbcMccRole = role;
        tag.dataset.hsbcMcc = mcc;
        tag.textContent = mcc;
        tag.setAttribute('aria-label', titleFor(mcc));
        return tag;
    }

    function syncTags(cell, tx, key) {
        const { mcc } = tx;
        const desktop = cell.querySelector('[data-hsbc-mcc-role="desktop"]');
        const mobile = cell.querySelector('[data-hsbc-mcc-role="mobile"]');
        if (cell.dataset.hsbcMcc === mcc && desktop && mobile) {
            for (const tag of [desktop, mobile]) {
                tag.setAttribute('aria-label', titleFor(mcc));
            }
            cell.dataset.hsbcMccSignature = key;
            cell.dataset.hsbcMccTransactionId = tx.id;
            cell.dataset.hsbcMccRef = tx.ref;
            return;
        }

        removeTags(cell);
        cell.insertBefore(makeTag(mcc, 'desktop'), cell.firstChild);
        cell.appendChild(makeTag(mcc, 'mobile'));
        cell.dataset.hsbcMcc = mcc;
        cell.dataset.hsbcMccSignature = key;
        cell.dataset.hsbcMccTransactionId = tx.id;
        cell.dataset.hsbcMccRef = tx.ref;
    }


    function pendingRowParts(row) {
        const desc = row.querySelector('td[data-label="Description"]');
        const type = row.querySelector('td[data-label="Type"]');
        if (!desc || !type) return null;
        return {
            desc: desc.textContent.trim(),
            cell: type,
        };
    }

    function makePredictedTag(prediction, role) {
        const tag = makeTag(prediction.mcc, role);
        tag.classList.add('hsbc-mcc-predicted');
        tag.dataset.hsbcMccPredicted = '1';
        tag.dataset.hsbcMccHits = String(prediction.hits);
        tag.dataset.hsbcMccTotal = String(prediction.total);
        tag.dataset.hsbcMccConfidence = prediction.confidence;
        tag.textContent = `~${prediction.mcc}`;
        tag.setAttribute(
            'aria-label',
            `Predicted MCC ${prediction.mcc}. ${prediction.hits}/${prediction.total} matching posted transactions. ${titleFor(prediction.mcc)}`,
        );
        return tag;
    }

    function syncPrediction(cell, prediction) {
        if (!prediction) {
            removeTags(cell);
            return;
        }

        const desktop = cell.querySelector('[data-hsbc-mcc-role="desktop"]');
        const mobile = cell.querySelector('[data-hsbc-mcc-role="mobile"]');
        const same = (
            cell.dataset.hsbcMcc === prediction.mcc
            && cell.dataset.hsbcMccPredicted === '1'
            && desktop
            && mobile
            && desktop.dataset.hsbcMccHits === String(prediction.hits)
            && desktop.dataset.hsbcMccTotal === String(prediction.total)
        );
        if (same) return;

        removeTags(cell);
        const desktopTag = makePredictedTag(prediction, 'desktop');
        const mobileTag = makePredictedTag(prediction, 'mobile');
        cell.insertBefore(desktopTag, cell.firstChild);
        cell.appendChild(mobileTag);
        cell.dataset.hsbcMcc = prediction.mcc;
        cell.dataset.hsbcMccPredicted = '1';
    }

    function syncFx(cell, tx) {
        const fx = tx.fx;
        if (!fx?.originalAmount) {
            removeFx(cell);
            return;
        }

        const text = `${fx.currency} ${fx.originalAmount}`;
        const existing = cell.querySelector('[data-hsbc-fx-role="original"]');
        if (existing && cell.dataset.hsbcFxTransactionId === tx.id && existing.textContent === text) return;

        removeFx(cell);
        const amount = cell.querySelector('.trans_amt');
        if (!amount) return;

        const original = document.createElement('span');
        original.className = 'hsbc-original-amount';
        original.dataset.hsbcFxRole = 'original';
        original.textContent = text;

        const details = [
            `Original amount: ${text}`,
            fx.exchangeRate ? `Exchange rate: ${fx.exchangeRate}` : null,
            fx.currencyName && fx.currencyName !== fx.currency ? fx.currencyName : null,
        ].filter(Boolean).join('. ');
        original.setAttribute('aria-label', details);

        amount.insertAdjacentElement('afterend', original);
        cell.dataset.hsbcFxTransactionId = tx.id;
    }

    function draw() {
        // Rebuild occurrence queues on every draw. This makes redraws idempotent
        // while still pairing duplicate signature rows one-to-one.
        const queues = new Map(
            [...posted].map(([key, bucket]) => [key, bucket.slice()])
        );

        for (const row of document.querySelectorAll('#acc_table tr.account_pg_new')) {
            const p = rowParts(row);
            if (!p) continue;
            const key = signature(p.date, p.amount, p.type, p.desc);
            const queue = key ? queues.get(key) : null;
            const tx = queue?.shift();

            if (!tx) {
                removeTags(p.cell);
                removeFx(p.amountCell);
                continue;
            }

            syncTags(p.cell, tx, key);
            syncFx(p.amountCell, tx);
        }

        for (const row of document.querySelectorAll('#transaction-history .pending_table tbody tr')) {
            const p = pendingRowParts(row);
            if (!p) continue;
            syncPrediction(p.cell, predictMcc(p.desc));
        }
    }

    function scheduleDraw() {
        if (drawPending) return;
        drawPending = true;
        const run = () => {
            drawPending = false;
            draw();
        };
        typeof requestAnimationFrame === 'function' ? requestAnimationFrame(run) : setTimeout(run, 0);
    }

    function installStyles() {
        if (document.getElementById('hsbc-mcc-style')) return;
        const style = document.createElement('style');
        style.id = 'hsbc-mcc-style';
        style.textContent = `
            .hsbc-mcc-tag{color:#db0011;cursor:help;font-size:.92em;font-weight:600;white-space:nowrap}
            .hsbc-mcc-desktop{display:none}
            .hsbc-mcc-mobile{display:inline;margin-left:6px}
            .hsbc-mcc-predicted{font-style:italic;opacity:.68;pointer-events:auto!important}
            .hsbc-original-amount{display:block;margin-top:2px;color:#666;font-size:.78em;font-weight:400;line-height:1.2;white-space:nowrap}
            @media (min-width:770px){.hsbc-mcc-desktop{display:inline;margin-right:8px}.hsbc-mcc-mobile{display:none}}
            #hsbc-mcc-tooltip{position:fixed;z-index:2147483647;display:none;max-width:360px;padding:7px 9px;border:1px solid rgba(0,0,0,.18);border-radius:4px;background:#fff;color:#222;box-shadow:0 2px 8px rgba(0,0,0,.18);font:12px/1.4 Arial,sans-serif;pointer-events:none}
            #hsbc-mcc-tooltip b{display:block}
            #hsbc-mcc-tooltip small{display:block;margin-top:3px;color:#666}
        `;
        document.head.appendChild(style);
    }

    function ensureTooltip() {
        if (tooltip?.isConnected) return tooltip;
        tooltip = document.createElement('div');
        tooltip.id = 'hsbc-mcc-tooltip';
        document.body.appendChild(tooltip);
        return tooltip;
    }

    function positionTooltip(anchor) {
        if (!tooltip || tooltip.style.display === 'none' || !anchor) return;

        const gap = 8;
        const margin = 8;
        const a = anchor.getBoundingClientRect();
        const r = tooltip.getBoundingClientRect();

        let left = a.left + (a.width - r.width) / 2;
        left = Math.min(
            Math.max(margin, left),
            Math.max(margin, innerWidth - r.width - margin),
        );

        let top = a.top - r.height - gap;
        if (top < margin) {
            top = a.bottom + gap;
        }
        if (top + r.height > innerHeight - margin) {
            top = Math.max(margin, innerHeight - r.height - margin);
        }

        tooltip.style.left = `${Math.round(left)}px`;
        tooltip.style.top = `${Math.round(top)}px`;
    }

    function installTooltip() {
        document.addEventListener('mouseover', (event) => {
            const tag = event.target.closest?.('.hsbc-mcc-tag');
            const mcc = tag ? normMcc(tag.dataset.hsbcMcc) : null;
            if (!mcc) return;

            const box = ensureTooltip();
            const predicted = tag.dataset.hsbcMccPredicted === '1';
            const code = document.createElement('b');
            code.textContent = predicted ? `Predicted MCC ~${mcc}` : `MCC ${mcc}`;
            const desc = document.createElement('div');
            desc.textContent = mccDb[mcc] ?? 'Description unavailable';
            const meta = document.createElement('small');
            if (predicted) {
                const hits = Number(tag.dataset.hsbcMccHits) || 0;
                const total = Number(tag.dataset.hsbcMccTotal) || 0;
                const confidence = tag.dataset.hsbcMccConfidence || 'low';
                const percent = total ? Math.round((hits / total) * 100) : 0;
                meta.textContent = `Prediction only · ${hits}/${total} matching posted transactions (${percent}%, ${confidence} confidence)`;
            } else {
                meta.textContent = mccMeta?.version ? `Mastercard MCC DB ${mccMeta.version}` : 'Mastercard MCC DB not loaded';
            }
            box.replaceChildren(code, desc, meta);
            box.style.display = 'block';
            positionTooltip(tag);
        });

        document.addEventListener('mouseout', (event) => {
            const tag = event.target.closest?.('.hsbc-mcc-tag');
            if (!tag || !tooltip) return;
            if (event.relatedTarget && tag.contains(event.relatedTarget)) return;
            tooltip.style.display = 'none';
        });

        window.addEventListener('scroll', () => {
            if (tooltip) tooltip.style.display = 'none';
        }, true);
        window.addEventListener('resize', () => {
            if (tooltip) tooltip.style.display = 'none';
        });
    }

    function installObserver() {
        const root = document.querySelector('#react-container') ?? document.body;
        if (!root) return;
        new MutationObserver(() => scheduleDraw()).observe(root, { childList: true, subtree: true });
    }

    function validDb(db) {
        if (!db || db.schemaVersion !== DB_SCHEMA || !db.mcc || typeof db.mcc !== 'object' || Array.isArray(db.mcc)) return false;
        const entries = Object.entries(db.mcc);
        return entries.length >= 300 && entries.every(([code, desc]) =>
            /^\d{4}$/.test(code) && typeof desc === 'string' && desc.trim()
        );
    }

    function applyDb(db) {
        mccDb = Object.freeze({ ...db.mcc });
        mccMeta = Object.freeze({ version: String(db.version ?? ''), source: db.source ?? null });
        scheduleDraw();
    }

    function clearDbValidators() {
        GM_setValue(STORE.etag, '');
        GM_setValue(STORE.modified, '');
    }

    function loadCachedDb() {
        const raw = GM_getValue(STORE.db, '');
        if (!raw) {
            clearDbValidators();
            return;
        }

        try {
            const db = JSON.parse(raw);
            if (validDb(db)) {
                applyDb(db);
                return;
            }
        } catch (error) {
            log('Ignoring invalid cached MCC DB:', error);
        }

        clearDbValidators();
    }

    function responseHeaders(raw) {
        const out = Object.create(null);
        for (const line of String(raw ?? '').split(/\r?\n/)) {
            const i = line.indexOf(':');
            if (i > 0) out[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
        }
        return out;
    }

    function refreshDb(force = false) {
        const now = Date.now();
        const success = Number(GM_getValue(STORE.success, 0)) || 0;
        const attempt = Number(GM_getValue(STORE.attempt, 0)) || 0;
        if (!force && ((success && now - success < REFRESH_MS) || (attempt && now - attempt < RETRY_MS))) return;
        GM_setValue(STORE.attempt, now);

        const headers = {};
        const etag = GM_getValue(STORE.etag, '');
        const modified = GM_getValue(STORE.modified, '');
        if (etag) headers['If-None-Match'] = etag;
        if (modified) headers['If-Modified-Since'] = modified;

        GM_xmlhttpRequest({
            method: 'GET',
            url: DB_URL,
            headers,
            anonymous: true,
            timeout: 15000,
            onload(response) {
                if (response.status === 304) {
                    GM_setValue(STORE.success, Date.now());
                    return;
                }
                if (response.status !== 200) return log('MCC DB HTTP', response.status);
                try {
                    const db = JSON.parse(response.responseText);
                    if (!validDb(db)) throw new Error('schema/content validation failed');
                    const h = responseHeaders(response.responseHeaders);
                    GM_setValue(STORE.db, JSON.stringify(db));
                    GM_setValue(STORE.success, Date.now());
                    if (h.etag) GM_setValue(STORE.etag, h.etag);
                    if (h['last-modified']) GM_setValue(STORE.modified, h['last-modified']);
                    applyDb(db);
                    log('Updated MCC DB to', db.version);
                } catch (error) {
                    log('Rejected remote MCC DB:', error);
                }
            },
            onerror: (error) => log('MCC DB refresh failed:', error),
            ontimeout: () => log('MCC DB refresh timed out'),
        });
    }

    function registerMenu() {
        GM_registerMenuCommand('Refresh Mastercard MCC database', () => refreshDb(true));
        GM_registerMenuCommand('Show Mastercard MCC database status', () => {
            alert([
                `Version: ${mccMeta?.version || 'not loaded'}`,
                `Entries: ${Object.keys(mccDb).length}`,
                `Last successful refresh: ${GM_getValue(STORE.success, 0) ? new Date(GM_getValue(STORE.success, 0)).toLocaleString() : 'never'}`,
                `Prediction merchant descriptions: ${Object.keys(merchantHistory.merchants).length}`,
                `Prediction observations: ${Object.keys(merchantHistory.seen).length}`,
            ].join('\n'));
        });
        GM_registerMenuCommand('Clear pending MCC prediction history', () => {
            if (!confirm('Clear locally learned merchant-to-MCC history?')) return;
            merchantHistory = emptyHistory();
            saveMerchantHistory();
            scheduleDraw();
        });
    }

    function initUi() {
        installStyles();
        ensureTooltip();
        installTooltip();
        installObserver();
        scheduleDraw();
    }

    loadMerchantHistory();
    installNetworkHook();
    installDownloadHook();
    loadCachedDb();
    refreshDb();
    registerMenu();

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initUi, { once: true });
    } else {
        initUi();
    }
})();
