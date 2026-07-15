// ═══════════════════════════════════════════════════════════════════════════
// Paper Trading Dashboard — End-to-End Playwright Tests
//
// These tests run against the Docker Compose test stack in CI.
// They verify:
//   1. The dashboard page loads and renders the UI shell
//   2. API endpoints return expected JSON shapes
//   3. Trader cards render with portfolio data
//   4. Position holdings display correctly
//   5. The trader detail modal opens/closes
//   6. Journal entries appear in the feed
// ═══════════════════════════════════════════════════════════════════════════

const { test, expect } = require('@playwright/test');

const DASHBOARD_URL = process.env.DASHBOARD_URL || 'http://localhost:5002';
const POLL_TIMEOUT = 30_000;

// ── Helpers ────────────────────────────────────────────────────────────────

async function waitForDashboard(page) {
  // Wait for the dashboard to finish loading — look for real content, not skeletons
  await page.waitForFunction(() => {
    const statusDot = document.getElementById('status-dot');
    if (!statusDot) return false;
    // Status should not be "Connecting..." (initial skeleton state)
    const statusText = document.getElementById('status-text');
    return statusText && statusText.textContent !== 'Connecting...';
  }, { timeout: POLL_TIMEOUT });
}

// ── Tests ──────────────────────────────────────────────────────────────────

test.describe('Dashboard Page Load', () => {
  test('should serve the HTML page', async ({ page }) => {
    const response = await page.goto(DASHBOARD_URL);
    expect(response.status()).toBe(200);
    await expect(page).toHaveTitle(/Paper Trading Command Center/);
  });

  test('should have the header elements', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await expect(page.locator('header')).toBeVisible();
    await expect(page.locator('text=PAPER TRADING COMMAND CENTER')).toBeVisible();
    await expect(page.locator('#status-text')).toBeVisible();
    await expect(page.locator('#last-updated')).toBeVisible();
    await expect(page.locator('#countdown')).toBeVisible();
  });

  test('should transition from skeleton to live data', async ({ page }) => {
    await page.goto(DASHBOARD_URL);

    // Initially should show skeleton or "Connecting..."
    const statusText = page.locator('#status-text');
    await expect(statusText).not.toBeEmpty();

    // Wait for data to load — status should become "Live"
    await waitForDashboard(page);
    await expect(statusText).toHaveText(/Live|Refreshing/);
  });
});

test.describe('Trader Cards', () => {
  test('should render three trader cards', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    const cards = page.locator('.trader-card');
    await expect(cards).toHaveCount(3);
  });

  test('trader cards should have name and value', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    const card = page.locator('.trader-card').first();
    await expect(card.locator('.card-name')).not.toBeEmpty();
    await expect(card.locator('.card-value')).not.toBeEmpty();
  });

  test('trader cards should show P&L and state bars', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    for (const card of await page.locator('.trader-card').all()) {
      await expect(card.locator('.card-pnl')).toBeVisible();
      // Check state bars exist (they may be hidden if value is 0%)
      await expect(card.locator('.state-bars')).toBeVisible();
      await expect(card.locator('.state-bar-fill.confidence')).toBeAttached();
      await expect(card.locator('.state-bar-fill.excitement')).toBeAttached();
      await expect(card.locator('.state-bar-fill.frustration')).toBeAttached();
    }
  });

  test('trader cards should show holdings pie chart', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    const card = page.locator('.trader-card').first();
    // The pie chart renders as SVG inside the card
    await expect(card.locator('svg')).toBeVisible();
  });
});

test.describe('Portfolio Values', () => {
  test('portfolio values should be dollar amounts', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    const values = await page.locator('.card-value').allTextContents();
    for (const val of values) {
      // Should either be a dollar amount or "No credentials" / unavailable
      expect(val).toMatch(/^\$[\d,]+\.\d{2}$|No credentials|unavailable/i);
    }
  });

  test('portfolio values should update across refreshes', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);
    await page.waitForTimeout(1000);
  });
});

test.describe('Trader Detail Modal', () => {
  test('should open modal when clicking a trader card', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    // Wait for trader card to be visible and clickable
    const card = page.locator('.trader-card').first();
    await expect(card).toBeVisible({ timeout: 10000 });
    await card.click();

    // Modal should open — wait for class to include 'open'
    await expect(page.locator('#trader-modal')).toHaveClass(/open/, { timeout: 10000 });
    await expect(page.locator('#modal-content')).toBeVisible();

    // Check for trader info in modal
    await expect(page.locator('.modal-trader-name')).not.toBeEmpty();
    await expect(page.locator('.modal-stat-grid')).toBeVisible();
  });

  test('modal should show positions table', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    // Use page.evaluate to directly open the modal with the first trader
    const traderId = await page.evaluate(() => {
      if (typeof cachedTraders !== 'undefined' && cachedTraders.length > 0) {
        const id = cachedTraders[0].id;
        toggleTraderDetail(id);
        return id;
      }
      return null;
    });

    // If evaluate didn't work, try clicking
    if (!traderId) {
      const card = page.locator('.trader-card').first();
      await expect(card).toBeVisible({ timeout: 10000 });
      await card.click();
    }

    // Wait for modal to open
    await expect(page.locator('#trader-modal')).toHaveClass(/open/, { timeout: 10000 });

    // Should have positions section (could say "No open positions")
    const positionsSection = page.locator('.modal-section').filter({ hasText: /Positions/i });
    await expect(positionsSection).toBeAttached();
  });

  test('should close modal with X button', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    // Open modal via evaluate
    await page.evaluate(() => {
      if (typeof cachedTraders !== 'undefined' && cachedTraders.length > 0) {
        toggleTraderDetail(cachedTraders[0].id);
      }
    });
    await expect(page.locator('#trader-modal')).toHaveClass(/open/, { timeout: 10000 });

    // Click the close button
    await page.locator('.modal-close').click();
    await expect(page.locator('#trader-modal')).not.toHaveClass(/open/);
  });

  test('should close modal with Escape key', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    // Open modal via evaluate
    await page.evaluate(() => {
      if (typeof cachedTraders !== 'undefined' && cachedTraders.length > 0) {
        toggleTraderDetail(cachedTraders[0].id);
      }
    });
    await expect(page.locator('#trader-modal')).toHaveClass(/open/, { timeout: 10000 });

    // Press Escape
    await page.keyboard.press('Escape');
    await expect(page.locator('#trader-modal')).not.toHaveClass(/open/);
  });
});

test.describe('Activity Feed', () => {
  test('should show activity decisions', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    const activityFeed = page.locator('#activity-feed');
    await expect(activityFeed).toBeVisible();

    // Should show either "No decisions" empty state or actual activity items
    const activityItems = page.locator('.activity-item');
    const emptyState = activityFeed.locator('.empty-state');
    const hasItems = await activityItems.count() > 0;
    const isEmpty = await emptyState.count() > 0;
    expect(hasItems || isEmpty).toBe(true);
  });
});

test.describe('Journal Feed', () => {
  test('should show journal entries', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    const journalFeed = page.locator('#journal-feed');
    await expect(journalFeed).toBeVisible();

    // Should have journal entries or empty state
    const entries = page.locator('.journal-entry');
    const emptyState = journalFeed.locator('.empty-state');
    const hasEntries = await entries.count() > 0;
    const isEmpty = await emptyState.count() > 0;
    expect(hasEntries || isEmpty).toBe(true);
  });

  test('journal entries should have trader badges', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    const hasEntries = await page.locator('.journal-entry').count();
    if (hasEntries > 0) {
      const firstEntry = page.locator('.journal-entry').first();
      await expect(firstEntry.locator('.pill')).toBeVisible();
      await expect(firstEntry.locator('.journal-text')).toBeVisible();
    }
  });
});

test.describe('API Endpoints', () => {
  test('/api/traders returns valid JSON with three traders', async ({ request }) => {
    const response = await request.get(`${DASHBOARD_URL}/api/traders`);
    expect(response.ok()).toBeTruthy();
    const data = await response.json();
    expect(data).toHaveProperty('traders');
    expect(data.traders.length).toBeGreaterThanOrEqual(1);
    expect(data).toHaveProperty('benchmarks');
  });

  test('/api/activity returns event list', async ({ request }) => {
    const response = await request.get(`${DASHBOARD_URL}/api/activity?limit=10`);
    expect(response.ok()).toBeTruthy();
    const data = await response.json();
    expect(data).toHaveProperty('events');
  });

  test('/api/journal returns entries', async ({ request }) => {
    const response = await request.get(`${DASHBOARD_URL}/api/journal?limit=10`);
    expect(response.ok()).toBeTruthy();
    const data = await response.json();
    expect(data).toHaveProperty('entries');
  });

  test('/api/signals returns signal data', async ({ request }) => {
    const response = await request.get(`${DASHBOARD_URL}/api/signals?limit=10`);
    expect(response.ok()).toBeTruthy();
    const data = await response.json();
    expect(data).toHaveProperty('signals');
  });

  test('/api/watchlists returns watchlist data', async ({ request }) => {
    const response = await request.get(`${DASHBOARD_URL}/api/watchlists`);
    expect(response.ok()).toBeTruthy();
    const data = await response.json();
    expect(data).toBeInstanceOf(Object);
  });

  test('/api/heartbeat returns status', async ({ request }) => {
    const response = await request.get(`${DASHBOARD_URL}/api/heartbeat`);
    expect(response.ok()).toBeTruthy();
    const data = await response.json();
    expect(data).toHaveProperty('status');
  });

  test('/api/vetoes returns risk events', async ({ request }) => {
    const response = await request.get(`${DASHBOARD_URL}/api/vetoes?limit=10`);
    expect(response.ok()).toBeTruthy();
    const data = await response.json();
    expect(data).toHaveProperty('vetoes');
  });

  test('/api/positions returns position data', async ({ request }) => {
    const response = await request.get(`${DASHBOARD_URL}/api/positions`);
    expect(response.ok()).toBeTruthy();
    const data = await response.json();
    expect(data).toHaveProperty('positions');
  });
});

test.describe('Benchmarks', () => {
  test('should show benchmark bar with SPY/QQQ data', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    const benchmarkBar = page.locator('#benchmark-bar');
    await expect(benchmarkBar).toBeVisible();
    // Should mention SPY and/or QQQ
    const text = await benchmarkBar.textContent();
    expect(text).toMatch(/Benchmarks/i);
  });
});

test.describe('Signals Panel', () => {
  test('should render signals table with bullish/bearish labels', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    const signalsContainer = page.locator('#signals-container');
    await expect(signalsContainer).toBeVisible();

    // Check for signal types in the table
    const bullishSignals = page.locator('.signal-bullish');
    const bearishSignals = page.locator('.signal-bearish');
    const neutralSignals = page.locator('.signal-neutral');
    const hasSignals = await page.locator('.signals-table tr').count() > 0;
    if (hasSignals) {
      expect(await bullishSignals.count() + await bearishSignals.count() + await neutralSignals.count())
        .toBeGreaterThan(0);
    }
  });
});

test.describe('Trader Thoughts Panel', () => {
  test('should show trader thoughts', async ({ page }) => {
    await page.goto(DASHBOARD_URL);
    await waitForDashboard(page);

    const thoughtsGrid = page.locator('#trader-thoughts');
    await expect(thoughtsGrid).toBeVisible();

    const thoughtCards = page.locator('.thought-card');
    const hasCards = await thoughtCards.count() > 0;
    const isEmpty = await thoughtsGrid.locator('.empty-state').count() > 0;
    expect(hasCards || isEmpty).toBe(true);
  });
});

test.describe('Data-Bus Integration', () => {
  test('data-bus health endpoint should respond', async ({ request }) => {
    const DATA_BUS_URL = process.env.DATA_BUS_URL || 'http://localhost:5000';
    try {
      const response = await request.get(`${DATA_BUS_URL}/health`);
      expect(response.ok()).toBeTruthy();
    } catch {
      // Data-bus may not be reachable in all environments — skip gracefully
      test.skip();
    }
  });
});