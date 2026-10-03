// ==UserScript==
// @name         HSBC US Credit Card MCC Display
// @namespace    https://github.com/uselessbug/hsbc-us-mcc-display
// @version      4.2.0
// @description  Show Mastercard MCC beside posted HSBC US credit-card transactions.
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
    const REFRESH_MS = 7 * 24 * 60 * 60 * 1000;
    const RETRY_MS = 6 * 60 * 60 * 1000;
    const STORE = Object.freeze({
        db: 'hsbcMcc.mastercardDb',
        etag: 'hsbcMcc.mastercardDb.etag',
        modified: 'hsbcMcc.mastercardDb.lastModified',
        success: 'hsbcMcc.mastercardDb.lastSuccess',
        attempt: 'hsbcMcc.mastercardDb.lastAttempt',
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
    let mccDb = Object.create(null);
    let mccMeta = null;
    let drawPending = false;
    let tooltip = null;
    let latestRequest = 0;

    const log = (...args) => console.debug('[HSBC MCC]', ...args);

    function normDesc(value) {
        return String(value ?? '').normalize('NFKC').replace(/[^\p{L}\p{N}]/gu, '').toUpperCase();
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
        const next = new Map();
        const groups = new Map();

        for (const tx of list) {
            const id = String(tx?.transactionId ?? '');
            if (!id) continue;
            const group = groups.get(id) ?? [];
            group.push(tx);
            groups.set(id, group);
        }

        // Preserve the visible API order. For duplicate signatures HSBC's DOM
        // preserves the same within-day order, so draw() can consume each bucket
        // one occurrence at a time.
        for (const tx of list) {
            if (!tx?.transactionCode?.display) continue; // skip FX auxiliary rows
            const key = apiSignature(tx);
            const mcc = normMcc(tx?.merchantCategoryCode);
            if (!key || !mcc) continue;

            const id = String(tx?.transactionId ?? '');
            const group = (id && groups.get(id)) || [tx];
            const bucket = next.get(key) ?? [];
            bucket.push({
                id,
                mcc,
                fx: extractFx(group, tx),
            });
            next.set(key, bucket);
        }

        posted.clear();
        for (const [key, bucket] of next) posted.set(key, bucket);
        log('Loaded posted transactions:', [...next.values()].reduce((n, b) => n + b.length, 0));
        scheduleDraw();
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
        delete cell.dataset.hsbcMccAmbiguous;
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
        tag.title = titleFor(mcc);
        tag.setAttribute('aria-label', tag.title);
        return tag;
    }

    function syncTags(cell, tx, key) {
        const { mcc } = tx;
        const desktop = cell.querySelector('[data-hsbc-mcc-role="desktop"]');
        const mobile = cell.querySelector('[data-hsbc-mcc-role="mobile"]');
        if (cell.dataset.hsbcMcc === mcc && desktop && mobile) {
            for (const tag of [desktop, mobile]) {
                tag.title = titleFor(mcc);
                tag.setAttribute('aria-label', tag.title);
            }
            return;
        }

        removeTags(cell);
        cell.insertBefore(makeTag(mcc, 'desktop'), cell.firstChild);
        cell.appendChild(makeTag(mcc, 'mobile'));
        cell.dataset.hsbcMcc = mcc;
        cell.dataset.hsbcMccSignature = key;
        cell.dataset.hsbcMccTransactionId = tx.id;
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

    function positionTooltip(event) {
        if (!tooltip || tooltip.style.display === 'none') return;
        const gap = 12;
        const r = tooltip.getBoundingClientRect();
        let left = event.clientX + gap;
        let top = event.clientY + gap;
        if (left + r.width > innerWidth - 8) left = Math.max(8, event.clientX - r.width - gap);
        if (top + r.height > innerHeight - 8) top = Math.max(8, event.clientY - r.height - gap);
        tooltip.style.left = `${left}px`;
        tooltip.style.top = `${top}px`;
    }

    function installTooltip() {
        document.addEventListener('mouseover', (event) => {
            const tag = event.target.closest?.('.hsbc-mcc-tag');
            const mcc = tag ? normMcc(tag.dataset.hsbcMcc) : null;
            if (!mcc) return;

            const box = ensureTooltip();
            const code = document.createElement('b');
            code.textContent = `MCC ${mcc}`;
            const desc = document.createElement('div');
            desc.textContent = mccDb[mcc] ?? 'Description unavailable';
            const meta = document.createElement('small');
            meta.textContent = mccMeta?.version ? `Mastercard MCC DB ${mccMeta.version}` : 'Mastercard MCC DB not loaded';
            box.replaceChildren(code, desc, meta);
            box.style.display = 'block';
            positionTooltip(event);
        });
        document.addEventListener('mousemove', (event) => {
            if (event.target.closest?.('.hsbc-mcc-tag')) positionTooltip(event);
        });
        document.addEventListener('mouseout', (event) => {
            if (event.target.closest?.('.hsbc-mcc-tag') && tooltip) tooltip.style.display = 'none';
        });
    }

    function installObserver() {
        const root = document.querySelector('#react-container') ?? document.body;
        if (!root) return;
        new MutationObserver(() => posted.size && scheduleDraw()).observe(root, { childList: true, subtree: true });
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

    function loadCachedDb() {
        const raw = GM_getValue(STORE.db, '');
        if (!raw) return;
        try {
            const db = JSON.parse(raw);
            if (validDb(db)) applyDb(db);
        } catch (error) {
            log('Ignoring invalid cached MCC DB:', error);
        }
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
            ].join('\n'));
        });
    }

    function initUi() {
        installStyles();
        ensureTooltip();
        installTooltip();
        installObserver();
        scheduleDraw();
    }

    installNetworkHook();
    loadCachedDb();
    refreshDb();
    registerMenu();

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initUi, { once: true });
    } else {
        initUi();
    }
})();
