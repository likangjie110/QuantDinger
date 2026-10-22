# P2 browser acceptance — 2026-10-19

## Result and scope

The actual main SPA at `/#/factor-mining` passed the 12 required business-flow terminal assertions in Edge at 1440x900 and 390x844. The latest recheck is `p2-mock-accepted-scope-final`: 38/48 checks passed, with the remaining ten failed full-page axe scans. No violations matched the expanded P2 control selectors in that recheck. Overall acceptance remains **failed**, and the command exited 1; scope-specific corrections do not turn full-page accessibility green.

The independent verifier's edits belong only to `tools/frontend-verification/`. The original `verify.mjs`, `core.mjs`, `fixtures.mjs`, and package scripts are unchanged. Its initial investigation did not edit product code. The main agent subsequently corrected the reported UI defects, completed the final browser recheck, and updated this report. The final recheck is not presented as a second independent review. Backend/PostgreSQL evidence belongs to the main implementation plan; browser mock runs use no business database, real model or deployment.

Baseline: `docs/superpowers/specs/2026-10-19-factor-mining-p2-design.md`, `docs/superpowers/plans/2026-10-19-factor-mining-p2.md`, frontend module 06 and delivery/test documentation. The UI owner explicitly handed off its completed changes before browser execution.

## Actual SPA provenance

This is the repository's native Vite **development** execution of the uncommitted main-SPA working tree. `frontend-vue/package.json` declares `dev: vite` and `build: vite build`. `p2-vite-server.mjs` imports that checkout's installed Vite and its original `vite.config.js`; it changes only the loopback host, port, cache directory, and automatic-open setting. The cache stays under the tooling artifact directory. Existing repository plugins, shims, aliases, styles and environment definitions are retained.

The source chain was read and independently exercised:

1. `frontend-vue/index.html:241` imports `/src/main.js`.
2. `src/main.js` imports the original `App.vue`, `./router`, store, permission module and `./utils/request`, and mounts Vue at `#app`.
3. `src/router/index.js` uses the original `constantRouterMap` in hash mode. `src/config/router.config.js:73` routes `/factor-mining` to `@/views/factor-mining`.
4. The original page imports `SubmitJob.vue`, `JobList.vue` and `CandidateList.vue`. They import `@/api/factor-mining`, which imports the original `@/utils/request` Axios wrapper.

`node tools/frontend-verification/p2-entry-proof.mjs` exited 0. Its browser observed HTTP 200 module loads for the original main, App, router, router config, permission, factor-mining page, API and request modules; the mounted Vue instance reported `/factor-mining`, router mode `hash`, and the real workspace element. It then exercised the original jobs GET through intercepted HTTP. Evidence: `artifacts/p2-entry-proof/browser-entry.json`, including individual source hashes.

The original entry probe and original independent business run recorded the same 278-file runtime hash:

```text
HEAD: 87093cf5ca3f7276d1c56895f3bdbdf686fcf683
dirty: true
worktree: eae26640125d663161f384623e9155893bd458da63a89e8cf5234a0e2f5b599d
unchanged during each run: true
headBundleVerified: false
```

No HTML, router, request wrapper or product component is supplied by this harness. API responses and synthetic authentication are fixtures. This evidence does not validate the production build output, `vite preview`, an immutable HEAD bundle, or a deployed image. The owner's successful `pnpm run build` is separately reported evidence, not a build performed by this verifier.

## Red / green commands

From the repository root:

```powershell
node --test tools/frontend-verification/tests/p2.test.mjs
```

- Initial red: 7 failed because the new P2 modules did not exist.
- Initial green: 7 passed.
- A fresh browser run exposed four missing shell GET fixtures. The new regression test first produced 7 passed / 1 failed at `/api/settings/public-config`; after implementing only those explicit GET fixtures, 8 passed.

```powershell
node --test tools/frontend-verification/tests/core.test.mjs tools/frontend-verification/tests/p2.test.mjs
node --check tools/frontend-verification/verify-p2.mjs
node --check tools/frontend-verification/p2-vite-server.mjs
node --check tools/frontend-verification/p2-entry-proof.mjs
```

Initial helper regression: **20 passed** (12 original + 8 P2). During the final correction a target-coverage test first failed because `isP2Target` was missing. The shared detector now covers research summary/fingerprint and replay-submit children, and the helper regression is **21 passed** (12 original + 9 P2). The runner syntax check also exited 0. These checks validate the harness; they do not substitute for browser acceptance.

## Original independent browser command and evidence

```powershell
node tools/frontend-verification/verify-p2.mjs --source frontend-vue --mode mock --ui-ready true --browser-channel msedge --output-dir tools/frontend-verification/artifacts/p2-mock-final
```

Run: 2026-10-19 13:33:26–13:34:35 UTC. Browser: Edge 155.0.4283.62. Page language observed: English; theme: repository default `realdark`. All artifact paths below are relative to `tools/frontend-verification/artifacts/p2-mock-final/`.

| Evidence | Observed result |
| --- | --- |
| `browser-report.json` | 48 checks; 38 passed; 10 failed axe scans; 12/12 required business terminal assertions passed |
| `api-evidence.json` | Actual DOM-triggered estimate/run payloads, job receipts and candidate query reads |
| `console.json` | 0 console errors / page errors; 16 warnings retained in the log |
| `network.json` | 0 failed requests / HTTP errors; 0 blocked or unknown operations |
| `axe.json` | 76 rule records across 10 state/viewport scans; records include targets and failure summaries; these are not 76 unique defects |
| 16 PNGs | Desktop/mobile parameters, holdout input, research summary, replay preview/submit/result and filtered candidates |

Business assertions on **both** viewport sizes:

- Evolution controls expose defaults population 32, generations 20, elite ratio 0.25, with associated labels. Holdout defaults to 0.10, is changed through the actual input to 0.20, and reaches estimate and submit. Inactive AI fields are absent.
- The submitted job is opened through the actual jobs list. `summary.research` displays group, per-run and cumulative trials, context hash, freeze version, frozen snapshot hash, reserved/not-evaluated holdout and date boundaries. A full reload re-reads the same fixture result.
- The candidates action sets `tab=candidates&runId=<numeric id>`; the rendered candidate rows, filter control and API query retain that run after full reload.
- Recorded AI proposals open the explicit replay preview. A full reload resets the temporary replay selection. Submission sends exactly `{searchMode:'ai', replayJobId:'p2-recorded-ai'}` and does not estimate. The new job and its filtered candidates survive subsequent fixture reads and page reloads.
- Enumeration excludes evolution and AI parameters. All ten responsive measurements passed. The browser exercises the original forms, tabs, modals, routing and request wrapper.

Reviewed screenshot pairs:

- `evolution-parameters-1440x900.png` / `evolution-parameters-390x844.png`
- `evolution-input-1440x900.png` / `evolution-input-390x844.png`
- `evolution-research-1440x900.png` / `evolution-research-390x844.png` — scrolls the original modal to show the actual summary; hashes wrap on mobile.
- `replay-preview-1440x900.png` / `replay-preview-390x844.png`
- `replay-submit-1440x900.png` / `replay-submit-390x844.png`
- `submitted-candidates-1440x900.png` / `submitted-candidates-390x844.png`

## Original findings and owner handoff

The following findings describe the original red run. The independent verifier handed them to the main agent; the original handoff is retained at `tools/frontend-verification/DEWEY-P2-A11Y-HANDOFF.md`. Product corrections and final results are listed below. Surrounding-shell findings without a clean baseline comparison remain unclassified.

- `color-contrast: serious` on `label[for="fm-holdout-ratio"]`: 1.03:1 (required 4.5:1). The same light-panel/near-white label pattern also hits `label[title="Frozen ratio"]` at 1.03:1.
- `color-contrast: serious` on `.fm-replay .ant-descriptions-item-content`: 1.16:1; on `#fm-submit-replay > span`: 2.26:1 (required 4.5:1). The screenshot visibly confirms dark text on the dark configuration cells.
- `color-contrast: serious` on the research fingerprint `.fm-sub`: 1.12:1 (`#0f0f0f` on `#1c1c1c`, 12px; required 4.5:1). The source is `JobList.vue:164` with the base rule at `JobList.vue:592–596`.
- In `evolution-research` at `390x844`, `scrollable-region-focusable` targets are `.ant-table-wrapper` and `.ant-table-body`. This is a candidate table overflow-region finding, not a proven `#fm-research-summary` target. The report retains it even though the runner's P2-control selector count is zero for this generic rule.
- Full-page scans also report ARIA and naming issues on tabs, existing form controls, header and candidate filters, including critical `aria-required-children`, `aria-required-attr` and `label`. Without a clean baseline comparison, the report does not classify all surrounding-shell findings as pre-existing or regressions.

Earlier failed artifacts (`p2-mock-initial`, `second`, `third`, `fourth`, `fifth`, `diagnostic`) are retained as development evidence. Their language selectors, read-only date-picker handling, formatted `0.10` comparison, missing shell fixtures and opaque `about:blank` localStorage initialization were harness defects. The final run corrected them and showed zero errors; earlier failures are not attributed to product behavior.

## Final corrections and recheck

The main agent corrected the dark advanced-panel background, label/hint and replay-value contrast, numeric control accessible names, and research fingerprint hint. The replay button now derives a darker shade from the existing primary-color token; its scoped rule overrides the global important theme rule. Expanded target detection exposed the button's previously omitted child node before this last correction. No axe rule or failure threshold was relaxed.

The source is the unchanged real main-SPA working tree throughout the final run:

```text
HEAD: 87093cf5ca3f7276d1c56895f3bdbdf686fcf683
dirty: true
worktree before/after: e338396259c08ce448402e4bf7f1ece8a7602fae6d5f3f7bec9118372a96b668
runtime files: 278
headBundleVerified: false
```

Executed from the repository root (equivalently from the tooling directory with relative paths):

```powershell
node tools/frontend-verification/verify-p2.mjs --source frontend-vue --mode mock --ui-ready true --browser-channel msedge --output-dir tools/frontend-verification/artifacts/p2-mock-accepted-scope-final
```

Run: 2026-10-19 14:38:10–14:39:11 UTC, Edge 155.0.4283.62, English and the repository's dark theme. Exit 1 preserves full-page accessibility failures. Artifacts are under `artifacts/p2-mock-accepted-scope-final/`:

| Evidence | Latest result |
| --- | --- |
| `browser-report.json` | 48 checks, 38 passed / 10 full-page axe failed; business terminal assertions 12/12 passed |
| `api-evidence.json` | Actual form estimate/run payloads, minimal AI replay request, new job read and numeric run-filter reload verified |
| `console.json` / `network.json` | 0 console/page errors, 0 unexpected network errors and 0 prohibited operations |
| `axe.json` | 70 rule records across state/viewport scans; zero rows matched the expanded P2 selectors, including `fm-research-fingerprint` and `fm-submit-replay` |
| 16 PNGs | Desktop/mobile input, research, replay and candidates; no measured horizontal overflow or oversized dialog |

Manual visual inspection of `evolution-input-1440x900.png`, `evolution-research-390x844.png`, and `replay-submit-390x844.png` confirmed the dark panel, visible wrapped fingerprints and warning, readable replay values/button, and accessible mobile action placement. The automated layout checks cover both viewports. The other screenshots are retained for review; this is not a complete manual accessibility audit.

The full-page scans still report ARIA/naming issues on tabs and surrounding forms/header/filters, table focus and other contrast findings. A zero P2 selector count is limited evidence; it does not attribute all remaining findings or establish an accessible page. Full acceptance remains failed. Without a clean baseline comparison these remaining findings cannot all be labelled pre-existing or regressions.

## Integration boundary and uncovered items

`--mode mock` intercepts HTTP after the original request wrapper and retains synthetic fixture state in memory. A reload therefore establishes UI re-read behavior, not PostgreSQL durability, worker execution, research migration concurrency, or real replay determinism. All shell APIs and authentication are also fixtures. No business DB writes, real models, deployment, or production credentials were used.

`--mode test-backend --backend-url http://127.0.0.1:PORT` is a separate, **blocked** mode. Before any mining submission it requires a loopback test service to return `/__p2_verification_capabilities` with `schemaVersion:1`, `environment:'test'`, `businessDatabaseWrites:false`, `realModelCalls:false`, and `mutationScope:'factor-mining-fixtures'`. Browser authentication is stripped before forwarding; shell responses remain local fixtures. No such isolated test-backend endpoint was supplied in this task, and the verifier was instructed not to create one, so integration is blocked. Main-agent backend/real-PG results are separate evidence.

Additional uncovered items: production bundle and deployed image; other browser engines; Chinese/light-theme browser coverage; real authentication and permission boundaries; injected loading/empty/error/forbidden states for the new flows; invalid input boundaries beyond the owner's unit suite; full keyboard/screen-reader audit. Axe success would not replace manual accessibility testing.

The main agent reran recursive factor-mining Vue/JS lint, all four factor-mining unit modules (34 passed), and the production build after UI corrections. Its global lint remains 23 errors / 1261 warnings; global unit remains 519 passed / 1 failed at factorLibraryLocalization's missing backend checkout path, and the typecheck script is absent. These product checks and exact commands are recorded in the implementation plan; they are not browser integration evidence.

## Resume checkpoint

The harness, original independent investigation, product corrections and final main-agent recheck are recorded. No identified P2-selector violation remains in the final report. Full-page accessibility remains failed and real HTTP integration remains blocked by the absence of an isolated backend. No production/HEAD bundle or deployment acceptance is claimed. Further product edits require new source attribution and affected checks; there is no remaining backend/engine implementation task in this P2 checklist.
