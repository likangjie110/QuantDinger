## Summary

<!-- What does this PR do? -->

## Related issue

<!-- Use "Closes #123" for a complete scoped issue or "Part of #123" for an epic slice. -->

-

## Changes

- 

## Test plan

- [ ] Tested locally with `docker compose up -d --build`
- [ ] Backend logs show no errors
- [ ] Relevant pytest tests pass

## User-flow and frontend coverage

<!-- Start with doc/web_doc/07-feature-delivery-template.md. Backend-only scope needs a reason and frontend TODO. -->

| User action / requirement | Route and UI delivery | API and shared contract | State / permission coverage | Evidence or remaining gap |
| --- | --- | --- | --- | --- |
| | | | | |

- [ ] Frontend target, source repository/revision, development entry, and deployment target identified
- [ ] UI controls and API support checked separately; numbered module docs updated
- [ ] Existing components, tokens, authentication/request wrapper, and i18n reused
- [ ] Non-fixing lint, typecheck, unit tests, and build run with current results
- [ ] Actual application opened in a browser; main flow and five states checked

## API documentation (if routes/schemas changed)

- [ ] Regenerated `docs/api/openapi.yaml` (`cd backend_api_python && python scripts/export_openapi.py`)
- [ ] Updated `docs/agent/agent-openapi.json` if `/api/agent/v1` routes changed
- [ ] Breaking API changes called out below (oasdiff will fail CI otherwise)

## Screenshots (if UI change)

<!-- Route, E2E command, 1440x900 and 390x844 screenshots, console/network findings, axe results, and uncovered items. -->

| Layer | Status (not run / blocked / failed / passed) | Command and evidence |
| --- | --- | --- |
| Backend | | |
| Frontend lint / typecheck / unit / build | | |
| Mock browser UI | | |
| Test-backend integration | | |
| Deployment / running image revision | | |

<!-- Mock UI, standalone compatibility UI, test-backend integration, and deployment are separate scopes. Missing source or scripts must remain blocked. Do not paste tokens, cookies, private source, or unredacted request bodies. -->

## Roadmap contribution (if applicable)

- [ ] A maintainer confirmed the claimed scope before implementation
- [ ] This PR implements one reviewable slice rather than an entire epic
- [ ] User-facing behavior and compatibility notes are documented
