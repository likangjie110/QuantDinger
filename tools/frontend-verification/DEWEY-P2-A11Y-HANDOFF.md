# Dewey P2 a11y handoff

Target UI owner: Dewey `01a15430-e452-7171-8cdb-cd37aea20119`

This is the browser verifier's exact handoff for the current main-SPA working tree. The verifier did not edit `frontend-vue`. The direct `vtell` command/session bridge is unavailable in this host, so the file is retained for the owner/main agent to forward verbatim; it is not evidence that the message was delivered.

Evidence source:

- `tools/frontend-verification/artifacts/p2-mock-final/browser-report.json`
- `tools/frontend-verification/artifacts/p2-mock-final/axe.json`
- `tools/frontend-verification/artifacts/p2-mock-final/evolution-input-1440x900.png`
- `tools/frontend-verification/artifacts/p2-mock-final/evolution-research-390x844.png`
- `tools/frontend-verification/artifacts/p2-mock-final/replay-preview-1440x900.png`
- `tools/frontend-verification/artifacts/p2-mock-final/replay-preview-390x844.png`

## Required P2 fixes

1. `color-contrast` serious — `label[for="fm-holdout-ratio"]`, ratio **1.03:1** (`#fefefe` on `#fafafa`, 12px). This label is a P2 control label in the advanced evolution panel. Give the label a foreground/background pair that reaches WCAG AA **>= 4.5:1**. The same light-panel/near-white label pattern also hits `label[title="Frozen ratio"]` at **1.03:1** and should be corrected together.
2. `color-contrast` serious — the replay configuration value cells under `.fm-replay .ant-descriptions-item-content`, ratio **1.16:1** (`#0a0a0a` on `#1c1c1c`, 14px). This is visible in both replay preview viewports. Apply a dark-theme text token/override to the value cells so the text reaches **>= 4.5:1** while preserving light-theme behavior.
3. `color-contrast` serious — `#fm-submit-replay > span`, ratio **2.26:1** (`#ffffff` on `#52c41a`, 12px). Darken the primary background, or use a text/background pair that reaches **>= 4.5:1**. For example, white on `#237804` is approximately **5.59:1**; dark text on the existing green is another valid approach if it matches the product token policy.
4. `color-contrast` serious — research fingerprint hint rendered as `.fm-sub` in the job detail/research state, ratio **1.12:1** (`#0f0f0f` on `#1c1c1c`, 12px). The source is `JobList.vue:164` and the base rule is `JobList.vue:592–596`. Add a theme-aware readable hint color; do not remove the fingerprint explanation. Target **>= 4.5:1**.

## Related mobile axe finding

In `evolution-research` at `390x844`, `scrollable-region-focusable` serious targets are exactly **`.ant-table-wrapper`** and **`.ant-table-body`**. The finding is about the candidate table's overflow region: it should contain focusable content and itself be keyboard focusable with an accessible name where applicable. Do not report this as `#fm-research-summary` being the target. The runner's `p2Controls` selector count is zero for this generic table rule, but the axe violation remains and must stay in the report.

## Verification request

After the fix, hand off the stable working tree to the browser verifier. The verifier will run:

```powershell
node tools/frontend-verification/verify-p2.mjs --source frontend-vue --mode mock --ui-ready true --browser-channel msedge --output-dir tools/frontend-verification/artifacts/p2-mock-recheck
```

The recheck must retain the full axe scan and compare the affected targets at desktop `1440x900` and mobile `390x844`. Do not delete/skip axe rules or narrow the selector count. The existing `p2-mock-final` result remains the red evidence: business 12/12, console/network clean, axe gate failed.

