import { expect, test, type Page } from "@playwright/test";

// The reference request, driven through the real screens.
const REFERENCE = "I ordered two jackets. The blue one arrived damaged. Please refund just that one.";
const SHOTS = "../docs/walkthrough/screens";

async function signIn(page: Page, merchant: string, person: string) {
  await page.goto("/");
  const store = page.locator("section.card", { has: page.getByRole("heading", { name: merchant }) });
  await store.getByRole("button", { name: new RegExp(person) }).click();
}

async function say(page: Page, text: string) {
  await page.getByLabel("Message").fill(text);
  await page.getByRole("button", { name: "Send" }).click();
}

test.describe.configure({ mode: "serial" });

test("a shopper at Northline is refunded from one plain sentence", async ({ page }) => {
  await signIn(page, "Northline Supply", "Maya Okafor");
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByText("NL-1001")).toBeVisible();

  await say(page, REFERENCE);

  await expect(page.getByText("Your refund of $180.00 has been issued.")).toBeVisible();
  await expect(page.locator(".pill.status-refunded")).toHaveText("Refunded");
  await expect(page.getByText("Refund reference")).toBeVisible();
  // The green jacket keeps its full balance.
  await expect(page.getByText("$120.00 refundable")).toBeVisible();
  await expect(page.getByText("$0.00 refundable")).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/01-northline-refund.png`, fullPage: true });
});

test("the same sentence at Vale waits for a supervisor, who approves it", async ({ browser }) => {
  const shopper = await browser.newPage();
  await signIn(shopper, "Vale Studio", "Maya Okafor");
  await say(shopper, REFERENCE);
  await expect(shopper.locator(".pill.status-in_review")).toHaveText("In review");
  await shopper.screenshot({ path: `${SHOTS}/02-vale-in-review.png`, fullPage: true });

  const supervisor = await browser.newPage();
  await signIn(supervisor, "Vale Studio", "Vera Lindqvist");
  await expect(supervisor).toHaveURL(/\/staff$/);
  const review = supervisor.getByTestId("review-card");
  await expect(review).toContainText("Refund $180.00");
  await expect(review).toContainText("The value is above the automatic limit");
  await supervisor.screenshot({ path: `${SHOTS}/03-vale-supervisor-queue.png`, fullPage: true });
  await review.getByLabel("Decision note").fill("Photo shows the damage.");
  await review.getByRole("button", { name: "Approve" }).click();
  await expect(supervisor.getByText("Recorded: approved.")).toBeVisible();

  // The case panel shows the result, and the trace behind it.
  const panel = supervisor.getByTestId("case-panel");
  await expect(panel.locator(".pill.state-resolved")).toBeVisible();
  await panel.getByRole("button", { name: "Show trace" }).click();
  const trace = supervisor.getByTestId("trace");
  await expect(trace).toContainText("waiting for supervisor");
  await expect(trace).toContainText("processor.create_refund");
  await supervisor.screenshot({ path: `${SHOTS}/04-vale-approved-with-trace.png`, fullPage: true });

  // The shopper's screen catches up on its own.
  await expect(shopper.locator(".pill.status-refunded")).toHaveText("Refunded", { timeout: 20_000 });
  await shopper.close();
  await supervisor.close();
});

test("an exchange is arranged in the chat", async ({ page }) => {
  await signIn(page, "Northline Supply", "Maya Okafor");
  await page.getByRole("button", { name: "New request" }).click();

  await say(page, "The green jacket arrived damaged. Please exchange it.");
  await expect(page.getByText(/Which Trail Jacket would you like instead\?/)).toBeVisible();
  await page.getByLabel("Message").fill("a large");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.locator(".bubble", { hasText: "Your exchange is confirmed" })).toBeVisible();
  await expect(page.locator(".pill.status-exchanged")).toHaveText("Exchanged");
  await expect(page.getByText("Exchange reference")).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/05-northline-exchange.png`, fullPage: true });
});

test("an associate sends a case to review and reads the policies", async ({ browser }) => {
  const shopper = await browser.newPage();
  await signIn(shopper, "Northline Supply", "Omar Haddad");
  await say(shopper, "Can I return the tote? It arrived damaged.");
  await expect(shopper.locator(".pill.status-needs_your_confirmation")).toBeVisible();

  const associate = await browser.newPage();
  await signIn(associate, "Northline Supply", "Nina Park");
  await associate.getByRole("button", { name: /Canvas Tote/ }).first().click();
  const panel = associate.getByTestId("case-panel");
  await panel.getByLabel("Reason for review").fill("Second tote claim this month.");
  await panel.getByRole("button", { name: "Send to review" }).click();
  await expect(associate.getByText(/Sent to review/)).toBeVisible();
  await expect(panel.locator(".bubble.internal")).toContainText("Second tote claim this month.");
  await associate.screenshot({ path: `${SHOTS}/06-associate-send-to-review.png`, fullPage: true });

  // The shopper never sees the note, and their yes now goes to a supervisor.
  await shopper.getByRole("button", { name: "Yes, go ahead" }).click();
  await expect(shopper.locator(".pill.status-in_review")).toHaveText("In review");
  await expect(shopper.getByText("Second tote claim this month.")).toHaveCount(0);

  await associate.getByRole("link", { name: "Policies" }).click();
  await expect(associate).toHaveURL(/\/policies$/);
  await expect(associate.getByRole("heading", { name: /Return policy/ })).toBeVisible();
  await expect(associate.getByText("30-day returns")).toBeVisible();
  // An associate is not shown the supervisor-only approval rules.
  await expect(associate.getByText("Approval limit")).toHaveCount(0);
  await associate.screenshot({ path: `${SHOTS}/07-policies.png`, fullPage: true });
  await shopper.close();
  await associate.close();
});
