"use strict";

// Headlines and names come from public websites, so all text goes in through
// textContent and links must be https. Nothing here builds HTML from strings.
const STALE_SECONDS = 2 * 3600;
const REFRESH_MS = 5 * 60 * 1000;
let data = null;
let tab = "radar";

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === "class") node.className = value;
    else if (key === "href") { if (/^https:\/\//.test(value)) node.href = value; }
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

function pct(value) {
  if (value === null || value === undefined) return "n/a";
  return (value > 0 ? "+" : "") + value.toFixed(1) + "%";
}

function tone(value) {
  return value > 0 ? "num up" : value < 0 ? "num down" : "num";
}

function ago(ts) {
  const seconds = Math.max(0, Date.now() / 1000 - ts);
  if (seconds < 3600) return Math.round(seconds / 60) + " min ago";
  if (seconds < 86400) return Math.round(seconds / 3600) + " hr ago";
  return Math.round(seconds / 86400) + " d ago";
}

function compactDollars(value) {
  if (!value) return null;
  if (value >= 1e9) return "$" + (value / 1e9).toFixed(1) + "B";
  return "$" + (value / 1e6).toFixed(0) + "M";
}

function signalCard(s) {
  const details = [
    "Volume " + s.relative_volume.toFixed(1) + "x normal pace",
    "20 sessions " + pct(s.momentum_20d_pct),
    "Typical daily swing " + s.daily_volatility_pct.toFixed(1) + "%",
  ];
  const cap = compactDollars(s.market_cap);
  if (cap) details.push("Market cap " + cap);
  if (s.earnings_ts) details.push("Earnings " + new Date(s.earnings_ts * 1000).toLocaleDateString());

  const more = el("details", {},
    el("summary", {}, "Evidence"),
    el("ul", {}, ...s.reasons.map((r) => el("li", {}, r))),
    el("p", { class: "muted" }, details.join(" · ") + ". Points: attention " + s.categories.attention +
      ", price and volume " + s.categories.tape + ", insiders " + s.categories.insider + "."),
    s.headlines.length ? el("ul", {}, ...s.headlines.map((h) =>
      el("li", {}, el("a", { href: h.link, target: "_blank", rel: "noopener noreferrer" }, h.title)))) : null,
    el("div", { class: "links" },
      el("a", { href: "https://robinhood.com/stocks/" + s.symbol, target: "_blank", rel: "noopener noreferrer" }, "Robinhood"),
      el("a", { href: "https://finance.yahoo.com/quote/" + s.symbol, target: "_blank", rel: "noopener noreferrer" }, "Yahoo Finance"))
  );

  return el("article", { class: "card", id: "sig-" + s.symbol },
    el("div", { class: "row" },
      el("span", { class: "sym" }, s.symbol),
      el("span", { class: "num" }, "$" + s.price.toFixed(2) + " ", el("span", { class: tone(s.day_change_pct) }, pct(s.day_change_pct)))),
    el("div", { class: "row" },
      el("span", { class: "name" }, s.name),
      el("span", { class: "score" }, s.score + "/100")),
    el("div", { class: "bar" }, el("i", { style: "width:" + s.score + "%" })),
    el("div", { style: "font-size:14px" }, s.reasons.slice(0, 2).join(". ") || "No strong evidence right now"),
    s.flags.length ? el("div", { class: "flag" }, s.flags.slice(0, 2).join(". ")) : null,
    more);
}

function renderRadar(view) {
  if (data.stale) view.append(el("div", { class: "notice" }, "The latest scan found no usable data, so this list is from an earlier scan."));
  if (data.brief) {
    view.append(el("section", { class: "card" },
      el("strong", {}, "Claude's brief"),
      el("div", { class: "brief" }, data.brief.text),
      el("div", { class: "muted" }, "Written " + data.brief.slot + " ET from the data below. AI summaries can be wrong.")));
  }
  view.append(el("p", { class: "muted" }, "Ranked by unusual activity. A high score means a lot is happening, not that a stock will go up. Alerts fire at " + data.min_alert_score + "+."));
  for (const s of data.signals) view.append(signalCard(s));
}

function renderAlerts(view) {
  if (!data.alerts.length) {
    view.append(el("p", { class: "muted" }, "No alerts yet. They only fire while the market is open."));
    return;
  }
  for (const a of data.alerts) {
    view.append(el("article", { class: "card" },
      el("div", { class: "row" },
        el("span", { class: "sym" }, a.symbol),
        el("span", { class: "muted" }, ago(a.ts))),
      el("div", { class: "row" },
        el("span", { class: "muted" }, "Score " + a.score + " at $" + a.price.toFixed(2)),
        el("span", { class: tone(a.current_return_pct) }, pct(a.current_return_pct))),
      el("div", { class: "muted" }, "SPY over the same time: " + pct(a.current_spy_return_pct)),
      el("div", { style: "font-size:14px;margin-top:6px" }, a.reasons.slice(0, 2).join(". "))));
  }
}

function renderScore(view) {
  view.append(el("p", {}, "This is the honest test of the radar: did stocks do better than simply holding the S&P 500 (SPY) after an alert?"));
  const rows = data.scorecard.map((r) => el("tr", {},
    el("td", {}, r.horizon + (r.horizon === 1 ? " day" : " days")),
    el("td", { class: "n" }, String(r.count)),
    el("td", { class: "n" }, r.count ? pct(r.avg_return_pct) : "-"),
    el("td", { class: "n" }, r.count ? pct(r.avg_spy_return_pct) : "-"),
    el("td", { class: "n" }, r.count ? r.beat_spy_pct + "%" : "-"),
    el("td", { class: "n" }, r.count ? pct(r.worst_pct) : "-")));
  view.append(el("section", { class: "card" }, el("table", {},
    el("thead", {}, el("tr", {},
      el("th", {}, "After"), el("th", { class: "n" }, "Alerts"), el("th", { class: "n" }, "Avg"),
      el("th", { class: "n" }, "SPY"), el("th", { class: "n" }, "Beat"), el("th", { class: "n" }, "Worst"))),
    el("tbody", {}, ...rows))));
  view.append(el("p", { class: "muted" }, "Assumes buying at the alert price with no slippage, which flatters the result. Fewer than about 50 alerts tells you very little either way."));
}

function renderAbout(view) {
  view.append(el("section", { class: "card" },
    el("h2", { style: "margin-top:0" }, "What this is"),
    el("p", {}, "A research tool that ranks US stocks by unusual activity. It shows what people are talking about and buying, and the evidence behind each score. It does not know your finances and is not personalized investment advice. Neither this app nor Claude is a licensed advisor."),
    el("p", {}, "High-risk trading can lose all the money you put in, and most short-term traders lag the market. Check the Scorecard before trusting these signals.")));
  view.append(el("section", { class: "card" },
    el("h2", { style: "margin-top:0" }, "Where the data comes from"),
    el("ul", {},
      el("li", {}, "Reddit mentions: ApeWisdom"),
      el("li", {}, "Trending: Stocktwits and Yahoo Finance"),
      el("li", {}, "Prices, volume, headlines: Yahoo Finance, which can be delayed"),
      el("li", {}, "Insider purchases: SEC Form 4, filed up to 2 business days after the trade"))));
  const health = Object.entries(data.health).map(([name, value]) =>
    el("tr", {}, el("td", {}, name.replace(/_/g, " ")), el("td", { class: "n" }, String(value))));
  view.append(el("section", { class: "card" },
    el("h2", { style: "margin-top:0" }, "Last scan"),
    el("table", {}, el("tbody", {}, ...health))));
}

function render() {
  const view = document.getElementById("view");
  view.replaceChildren();
  for (const button of document.querySelectorAll("nav button")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === tab));
  }
  if (!data) {
    view.append(el("div", { class: "notice" }, "Could not load data. Check your connection, or wait for the first scan to finish."));
    return;
  }
  ({ radar: renderRadar, alerts: renderAlerts, score: renderScore, about: renderAbout })[tab](view);
}

function renderStatus() {
  const market = document.getElementById("market");
  market.replaceChildren(
    el("span", { class: "dot" + (data.market_open ? " open" : "") }),
    data.market_open ? "Market open" : "Market closed");
  const age = Date.now() / 1000 - data.generated_at;
  const updated = document.getElementById("updated");
  updated.textContent = "Updated " + ago(data.generated_at) + (age > STALE_SECONDS ? " (scanner may be down)" : "");
  updated.className = age > STALE_SECONDS ? "down" : "";
}

function jumpToAlertedStock() {
  const symbol = location.hash.slice(1);
  const card = symbol && document.getElementById("sig-" + symbol);
  if (card) {
    card.classList.add("target");
    card.scrollIntoView({ block: "start" });
  }
}

async function load(jump) {
  try {
    const response = await fetch("data.json?t=" + Date.now(), { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    data = await response.json();
    renderStatus();
  } catch (error) {
    if (data) return;
  }
  // A background refresh must not close the section someone is reading.
  if (jump || !document.querySelector("details[open]")) render();
  if (jump) jumpToAlertedStock();
}

for (const button of document.querySelectorAll("nav button")) {
  button.addEventListener("click", () => {
    tab = button.dataset.tab;
    render();
    window.scrollTo(0, 0);
  });
}
window.addEventListener("hashchange", () => {
  tab = "radar";
  render();
  jumpToAlertedStock();
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) load(false);
});
setInterval(() => load(false), REFRESH_MS);
load(true);
