import { test, expect, type Page, type Response, type TestInfo, type ConsoleMessage, type Request } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

type PageStep = (page: Page) => Promise<void>;
type ApiResponse = { method: string; url: string; status: number };
export type UIState = 'loading' | 'empty' | 'error' | 'success' | 'unauthorized' | 'forbidden';
const UI_STATES: UIState[] = ['loading', 'empty', 'error', 'success', 'unauthorized', 'forbidden'];

export type ExpectedResponse = ApiResponse & {
  reason: string;
  assertUI: PageStep;
  count?: number;
  // 仅在实测浏览器为该预期响应输出资源错误时配置；精确匹配文本和来源 URL。
  consoleErrors?: { text: string; url: string }[];
};

export type UserFlow = {
  name: string;
  entryPath: string;
  enter: PageStep;
  fill: PageStep;
  submit: PageStep;
  assertResult: PageStep;
  assertPersistedResult: PageStep;
  expectedResponses?: ExpectedResponse[];
} & ({
  expectedApi: ApiResponse;
  assertApi: (response: Response) => Promise<void>;
  apiNotApplicable?: never;
} | {
  expectedApi?: never;
  assertApi?: never;
  apiNotApplicable: { reason: string; evidence: string };
});

export type RouteCase = {
  name: string;
  path: string;
  setup?: PageStep;
  ready: PageStep;
  expectedResponses?: ExpectedResponse[];
};

export type StateCase = {
  route: string;
  name: string;
  state: UIState;
  path: string;
  setup: PageStep;
  assertState: PageStep;
  expectedResponses?: ExpectedResponse[];
};

export type AcceptanceConfig = {
  routes: RouteCase[];
  userFlow: UserFlow;
  stateCases: StateCase[];
  notApplicable?: Record<string, Partial<Record<UIState, { reason: string; evidence: string }>>>;
};

// 项目接入点：读取真实路由、组件、契约与脚本后填写。未接入时测试必须失败。
const acceptance: AcceptanceConfig | undefined = undefined;

function requireText(value: string | undefined, name: string) {
  if (!value?.trim()) throw new Error(`FRONTEND_ACCEPTANCE_INVALID: ${name} 必须填写项目事实`);
}

function requireStep(value: unknown, name: string) {
  if (typeof value !== 'function') throw new Error(`FRONTEND_ACCEPTANCE_INVALID: 缺少 ${name} 业务步骤或断言`);
}

function validateApiResponse(rule: ApiResponse) {
  requireText(rule?.method, 'method');
  requireText(rule?.url, '完整 API URL');
  const url = new URL(rule.url);
  if (!['http:', 'https:'].includes(url.protocol) || url.hash || rule.method !== rule.method.toUpperCase()
    || !Number.isInteger(rule.status) || rule.status < 200 || rule.status > 599) {
    throw new Error('FRONTEND_ACCEPTANCE_INVALID: API 需大写 method、完整 HTTP(S) URL 和精确 status');
  }
}

export function validateAcceptanceConfig(config: AcceptanceConfig | undefined): AcceptanceConfig {
  if (!config) {
    throw new Error('FRONTEND_ACCEPTANCE_NOT_CONFIGURED: 请接入真实用户闭环、页面和六态用例，不能以 body 可见代替业务验收');
  }
  if (!config.routes?.length) throw new Error('FRONTEND_ACCEPTANCE_INVALID: routes 不得为空');
  const routeNames = new Set<string>();
  for (const route of config.routes) {
    requireText(route.name, 'route.name');
    requireText(route.path, 'route.path');
    requireStep(route.ready, 'route.ready');
    if (routeNames.has(route.name)) throw new Error(`FRONTEND_ACCEPTANCE_INVALID: route.name 重复 ${route.name}`);
    routeNames.add(route.name);
  }
  const flow = config.userFlow;
  requireText(flow?.name, 'userFlow.name');
  requireText(flow?.entryPath, 'userFlow.entryPath');
  for (const key of ['enter', 'fill', 'submit', 'assertResult', 'assertPersistedResult'] as const) {
    requireStep(flow?.[key], `userFlow.${key}`);
  }
  if (flow.expectedApi) {
    if (flow.apiNotApplicable) throw new Error('FRONTEND_ACCEPTANCE_INVALID: API 与 apiNotApplicable 互斥');
    validateApiResponse(flow.expectedApi);
    requireStep(flow.assertApi, 'userFlow.assertApi');
    if (flow.expectedApi.status >= 400) throw new Error('FRONTEND_ACCEPTANCE_INVALID: 主流程须验证成功 API，异常放 stateCases');
  } else {
    requireText(flow.apiNotApplicable?.reason, 'API 不适用理由 apiNotApplicable.reason');
    requireText(flow.apiNotApplicable?.evidence, 'API 不适用证据 apiNotApplicable.evidence');
    if (flow.assertApi) throw new Error('FRONTEND_ACCEPTANCE_INVALID: API 不适用时不得配置 assertApi');
  }
  for (const scenario of config.stateCases ?? []) {
    if (!routeNames.has(scenario.route)) throw new Error(`FRONTEND_ACCEPTANCE_INVALID: 状态用例引用未知路由 ${scenario.route}`);
    if (!UI_STATES.includes(scenario.state)) throw new Error(`FRONTEND_ACCEPTANCE_INVALID: 未知状态 ${scenario.state}`);
    requireText(scenario.name, 'stateCase.name');
    requireText(scenario.path, 'stateCase.path');
    requireStep(scenario.setup, 'stateCase.setup');
    requireStep(scenario.assertState, 'stateCase.assertState');
  }
  for (const route of config.routes) {
    for (const state of UI_STATES) {
      const covered = config.stateCases?.some((scenario) => scenario.route === route.name && scenario.state === state);
      const exception = config.notApplicable?.[route.name]?.[state];
      if (state === 'success' && exception) throw new Error(`FRONTEND_ACCEPTANCE_INVALID: ${route.name} success 不得标记不适用`);
      if (covered && exception) throw new Error(`FRONTEND_ACCEPTANCE_INVALID: ${route.name} ${state} 同时标记覆盖和不适用`);
      if (!covered) {
        if (state === 'success') throw new Error(`FRONTEND_ACCEPTANCE_INVALID: ${route.name} success 必须提供真实状态用例`);
        requireText(exception?.reason, `${route.name} ${state} 不适用理由`);
        requireText(exception?.evidence, `${route.name} ${state} 不适用证据`);
      }
    }
  }
  return config;
}

function matchesResponse(response: Response, rule: ApiResponse) {
  return response.request().method() === rule.method && response.url() === rule.url && response.status() === rule.status;
}

export function trackBrowserSignals(page: Page, expectedResponses: ExpectedResponse[] = []) {
  for (const rule of expectedResponses) {
    validateApiResponse(rule);
    if (rule.status < 400) throw new Error('FRONTEND_ACCEPTANCE_INVALID: expectedResponses 仅用于预期 4xx/5xx');
    requireText(rule.reason, 'expectedResponse.reason');
    requireStep(rule.assertUI, 'expectedResponse.assertUI');
    if (!Number.isInteger(rule.count ?? 1) || (rule.count ?? 1) < 1) {
      throw new Error('FRONTEND_ACCEPTANCE_INVALID: expectedResponse.count 必须为正整数，默认 1');
    }
    for (const error of rule.consoleErrors ?? []) {
      if (error.url !== rule.url || !error.text.startsWith(`Failed to load resource: the server responded with a status of ${rule.status} (`)) {
        throw new Error('FRONTEND_ACCEPTANCE_INVALID: consoleErrors 仅允许该精确请求的浏览器资源诊断');
      }
    }
  }
  const consoleErrors: { text: string; url: string }[] = [];
  const pageErrors: string[] = [];
  const failedRequests: string[] = [];
  const errorResponses: string[] = [];
  const badResponses: string[] = [];
  const expectedResponseMatches = expectedResponses.map((rule) => ({
    method: rule.method, url: rule.url, status: rule.status, reason: rule.reason,
    expectedCount: rule.count ?? 1, actualCount: 0,
  }));
  const onConsole = (message: ConsoleMessage) => {
    if (message.type() === 'error') consoleErrors.push({ text: message.text(), url: message.location().url });
  };
  const onPageError = (error: Error) => { pageErrors.push(error.message); };
  const onRequestFailed = (request: Request) => {
    failedRequests.push(`${request.method()} ${request.url()} ${request.failure()?.errorText ?? 'requestfailed'}`);
  };
  const onResponse = (response: Response) => {
    if (response.status() < 400) return;
    const description = `${response.status()} ${response.request().method()} ${response.url()}`;
    errorResponses.push(description);
    const index = expectedResponses.findIndex((rule) => matchesResponse(response, rule));
    if (index < 0) badResponses.push(description);
    else expectedResponseMatches[index].actualCount += 1;
  };
  page.on('console', onConsole);
  page.on('pageerror', onPageError);
  page.on('requestfailed', onRequestFailed);
  page.on('response', onResponse);

  return {
    consoleErrors, pageErrors, failedRequests, errorResponses, badResponses, expectedResponseMatches,
    async assertClean() {
      for (const [index, rule] of expectedResponses.entries()) {
        expect(expectedResponseMatches[index].actualCount, `预期错误响应未发生或次数不符: ${rule.reason}`).toBe(rule.count ?? 1);
        await rule.assertUI(page);
      }
      for (const [index, rule] of expectedResponses.entries()) {
        expect(expectedResponseMatches[index].actualCount, `UI 断言后预期错误响应次数不符: ${rule.reason}`).toBe(rule.count ?? 1);
      }
      const unexpectedConsoleErrors = consoleErrors.filter((error) => !expectedResponses.some((rule, index) =>
        expectedResponseMatches[index].actualCount > 0 && rule.consoleErrors?.some((allowed) => allowed.text === error.text && allowed.url === error.url)));
      expect(unexpectedConsoleErrors, '意外控制台错误').toEqual([]);
      expect(pageErrors, '未处理页面异常').toEqual([]);
      expect(failedRequests, '网络传输失败').toEqual([]);
      expect(badResponses, '意外 4xx/5xx（包括 401/403）').toEqual([]);
    },
    dispose() {
      page.off('console', onConsole);
      page.off('pageerror', onPageError);
      page.off('requestfailed', onRequestFailed);
      page.off('response', onResponse);
    },
  };
}

export async function runClosedLoop(page: Page, flow: UserFlow) {
  await page.goto(flow.entryPath);
  await flow.enter(page);
  await flow.fill(page);
  const expectedApi = flow.expectedApi;
  if (expectedApi) {
    const [response] = await Promise.all([
      page.waitForResponse((candidate) => matchesResponse(candidate, expectedApi)),
      flow.submit(page),
    ]);
    await flow.assertApi(response);
  } else {
    requireText(flow.apiNotApplicable?.reason, 'API 不适用理由');
    requireText(flow.apiNotApplicable?.evidence, 'API 不适用证据');
    await flow.submit(page);
  }
  await flow.assertResult(page);
  await page.reload();
  await flow.assertPersistedResult(page);
}

async function screenshot(page: Page, testInfo: TestInfo, name: string) {
  const path = testInfo.outputPath('evidence', `${name.replace(/[^a-zA-Z0-9_-]/g, '_')}.png`);
  await page.screenshot({ path, fullPage: true });
  await testInfo.attach(name, { path, contentType: 'image/png' });
}

async function withSignals(page: Page, testInfo: TestInfo, expected: ExpectedResponse[] | undefined, run: () => Promise<void>) {
  const signals = trackBrowserSignals(page, expected);
  try {
    await run();
    await signals.assertClean();
  } finally {
    signals.dispose();
    await testInfo.attach('browser-signals', {
      body: Buffer.from(JSON.stringify(signals, null, 2)), contentType: 'application/json',
    });
  }
}

export function registerAcceptanceTests(config: AcceptanceConfig | undefined) {
  test.describe('前端用户闭环验收', () => {
    test('项目验收配置完整', async () => { validateAcceptanceConfig(config); });

    test('真实主流程、业务结果及刷新行为', async ({ page }, testInfo) => {
      const { userFlow } = validateAcceptanceConfig(config);
      await withSignals(page, testInfo, userFlow.expectedResponses, async () => {
        await runClosedLoop(page, userFlow);
        await screenshot(page, testInfo, 'after-refresh-business-result');
      });
    });

    for (const route of config?.routes ?? []) {
      test(`页面截图 ${route.name}`, async ({ page }, testInfo) => {
        validateAcceptanceConfig(config);
        await withSignals(page, testInfo, route.expectedResponses, async () => {
          await route.setup?.(page);
          await page.goto(route.path);
          await route.ready(page);
          await screenshot(page, testInfo, route.name);
        });
      });

      test(`页面可访问性 ${route.name} @a11y`, async ({ page }, testInfo) => {
        validateAcceptanceConfig(config);
        await withSignals(page, testInfo, route.expectedResponses, async () => {
          await route.setup?.(page);
          await page.goto(route.path);
          await route.ready(page);
          const results = await new AxeBuilder({ page }).analyze();
          expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact ?? '')),
            `${route.path} 可访问性问题`).toEqual([]);
        });
      });
    }

    for (const scenario of config?.stateCases ?? []) {
      test(`状态 ${scenario.route} ${scenario.state}: ${scenario.name}`, async ({ page }, testInfo) => {
        validateAcceptanceConfig(config);
        await withSignals(page, testInfo, scenario.expectedResponses, async () => {
          await scenario.setup(page);
          await page.goto(scenario.path);
          await scenario.assertState(page);
          await screenshot(page, testInfo, `${scenario.state}-${scenario.name}`);
        });
      });
    }
  });
}

registerAcceptanceTests(acceptance);
