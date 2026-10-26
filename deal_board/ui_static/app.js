// Deal Requirements Board: the broker-facing page.
// Calls this app's own POST /match and renders the result. Every value from the API is
// written with textContent, never parsed as HTML: the requirement can echo text from the post.
// Nothing the broker types is stored in the browser.
"use strict";

(() => {
  const MAX_CHARS = 4000;
  const REQUEST_TIMEOUT_MS = 45000; // the server caps one Claude call at 30 s plus one retry
  const MAX_FIT = 8; // property +3, transaction +2, market +2, deal size +1

  // Same order as deal_board/followup.py PRIORITY: the follow-up asks about the first missing one.
  const PRIORITY = ["state", "transaction_type", "property_type", "city", "deal_value_usd", "timeline"];

  const EXAMPLES = [
    { tag: "Complete post", text: "Need a buyer's agent in Phoenix, AZ for an SFR up to $450k, closing in 30 days." },
    { tag: "Missing the state", text: "Looking for a listing agent for a duplex in Portland, around $600k." },
    { tag: "Texas, public records", text: "Need a seller's agent in Dallas, TX for a retail strip center around $2.4M, closing in 60 days." },
    { tag: "Not a deal", text: "We are hiring a transaction coordinator for our Dallas office. DM me." },
  ];

  const STATES = {
    AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado",
    CT: "Connecticut", DE: "Delaware", DC: "District of Columbia", FL: "Florida", GA: "Georgia",
    HI: "Hawaii", ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky",
    LA: "Louisiana", ME: "Maine", MD: "Maryland", MA: "Massachusetts", MI: "Michigan", MN: "Minnesota",
    MS: "Mississippi", MO: "Missouri", MT: "Montana", NE: "Nebraska", NV: "Nevada", NH: "New Hampshire",
    NJ: "New Jersey", NM: "New Mexico", NY: "New York", NC: "North Carolina", ND: "North Dakota",
    OH: "Ohio", OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island",
    SC: "South Carolina", SD: "South Dakota", TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont",
    VA: "Virginia", WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming",
  };
  const PROPERTY = {
    single_family: "Single-family home", condo_townhome: "Condo or townhome",
    multifamily: "Multifamily (2+ units)", retail: "Retail", office: "Office",
    industrial: "Industrial or warehouse", land: "Land", mixed_use: "Mixed use", other: "Other",
  };
  const TRANSACTION = {
    buy: "Buy: the client is buying", sell: "Sell: the client is selling",
    lease: "Lease", "1031_exchange": "1031 exchange (replacement purchase)",
  };
  const TIMELINE = { within_30_days: "Within 30 days", "31_to_90_days": "31 to 90 days", over_90_days: "More than 90 days" };
  const FIELD_LABELS = {
    is_deal_request: "Deal request", state: "State", city: "City", property_type: "Property type",
    transaction_type: "Transaction", deal_value_usd: "Deal value", timeline: "Timeline", notes: "Notes",
  };
  // How a follow-up answer is added to the post, so the extractor reads it in context.
  const ANSWER_LEAD = {
    state: (a) => `The property is in ${a}.`,
    transaction_type: (a) => `The client wants to ${a}.`,
    property_type: (a) => `Property type: ${a}.`,
    city: (a) => `The property is in ${a}.`,
    deal_value_usd: (a) => `Price: ${a}.`,
    timeline: (a) => `Timing: ${a}.`,
  };

  const $ = (id) => document.getElementById(id);
  const form = $("match-form");
  const textarea = $("post-text");
  const field = $("post-field");
  const fieldError = $("post-error");
  const count = $("post-count");
  const submit = $("submit");
  const clearBtn = $("clear");
  const errorBox = $("error-summary");
  const errorTitle = $("error-title");
  const errorBody = $("error-body");
  const errorActions = $("error-actions");
  const reading = $("reading");
  const facts = $("facts");
  const provenance = $("provenance");
  const results = $("results");
  const resultsHeading = $("results-heading");
  const resultsBody = $("results-body");
  const introBody = resultsBody.cloneNode(true);
  const tplBroker = $("tpl-broker");

  let inFlight = null;
  let lastGood = null; // the last successful response, kept so a failed retry never wipes it

  const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const num = new Intl.NumberFormat("en-US");
  const longDate = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  // ---- Character count and field validation ---------------------------------------------

  function updateCount() {
    const n = textarea.value.length;
    count.textContent = `${num.format(n)} of ${num.format(MAX_CHARS)} characters`;
    count.classList.toggle("is-near", n > MAX_CHARS * 0.9);
    clearBtn.hidden = n === 0;
  }

  function setFieldError(message) {
    if (message) {
      field.classList.add("has-error");
      fieldError.textContent = message;
      fieldError.hidden = false;
      textarea.setAttribute("aria-invalid", "true");
      textarea.setAttribute("aria-describedby", "post-error post-hint post-count");
    } else {
      field.classList.remove("has-error");
      fieldError.hidden = true;
      fieldError.textContent = "";
      textarea.removeAttribute("aria-invalid");
      textarea.setAttribute("aria-describedby", "post-hint post-count");
    }
  }

  // ---- Error summary ---------------------------------------------------------------------

  function showError({ title, body, retry = false, fieldMessage = null }) {
    errorTitle.textContent = title;
    errorBody.textContent = body;
    errorActions.replaceChildren();
    if (fieldMessage) {
      setFieldError(fieldMessage);
      const link = el("a", null, "Go to the deal post");
      link.href = "#post-text";
      link.addEventListener("click", (e) => { e.preventDefault(); textarea.focus(); });
      errorActions.append(link);
    }
    if (retry) {
      const again = el("button", "button button--quiet", "Try again");
      again.type = "button";
      again.addEventListener("click", () => run(textarea.value));
      errorActions.append(again);
    }
    errorBox.hidden = false;
    errorBox.focus();
  }

  function hideError() {
    errorBox.hidden = true;
    setFieldError(null);
  }

  // Map every documented outcome of POST /match to plain language and a next step.
  function describeFailure(status, detail) {
    const d = typeof detail === "string" ? detail : "";
    if (status === 422 && d.startsWith("could not extract")) {
      return {
        title: "We couldn't read a deal from this post",
        body: "The extractor returned something that didn't fit the deal format, so nothing was matched. Try rewording the post: name the state, the city, the property and what the client wants to do.",
        fieldMessage: "Reword the post and run it again.",
      };
    }
    if (status === 422) {
      return { title: "Enter a deal post", body: "The post is empty or only spaces.", fieldMessage: "Enter a deal post." };
    }
    if (status === 413) {
      return { title: "This post is too long", body: `Shorten it to ${num.format(MAX_CHARS)} characters or fewer.`, fieldMessage: "Shorten the post." };
    }
    if (status === 503 && d === "extractor not configured") {
      return { title: "Matching isn't set up on this server", body: "The server has no extractor configured. The person running it needs to set one (see the README). Your post is still here.", retry: true };
    }
    if (status === 503) {
      return { title: "The matching service is unavailable", body: "This is usually temporary. Your post is still here; try again in a minute.", retry: true };
    }
    return { title: "Something went wrong on our side", body: `The server couldn't finish (error ${status}). Nothing was matched. Your post is still here; try again.`, retry: true };
  }

  // ---- Loading -----------------------------------------------------------------------------

  function setLoading(on) {
    results.setAttribute("aria-busy", on ? "true" : "false");
    submit.setAttribute("aria-disabled", on ? "true" : "false");
    const label = submit.querySelector(".button__label");
    const spinner = submit.querySelector(".button__spinner");
    if (on) {
      label.textContent = "Checking licenses…";
      if (!spinner) submit.prepend(el("span", "button__spinner"));
      resultsHeading.textContent = "Checking licenses…";
      const sk = el("div", "skeleton");
      sk.append(el("p", "status-text", "Reading the post, then checking each broker's license in the deal's state."));
      for (let i = 0; i < 3; i += 1) sk.append(el("div", "skeleton__row"));
      resultsBody.replaceChildren(sk);
    } else {
      label.textContent = "Find licensed brokers";
      if (spinner) spinner.remove();
    }
  }

  // ---- Rendering the requirement ------------------------------------------------------------

  function askedField(req, question) {
    if (!question) return null;
    return PRIORITY.find((f) => req.missing_fields.includes(f)) || null;
  }

  function factValue(key, req) {
    const v = req[key];
    if (key === "is_deal_request") return v ? "Yes" : "No";
    if (v === null || v === undefined || v === "") return null;
    if (key === "state") return STATES[v] ? `${STATES[v]} (${v})` : v;
    if (key === "property_type") return PROPERTY[v] || v;
    if (key === "transaction_type") return TRANSACTION[v] || v;
    if (key === "timeline") return TIMELINE[v] || v;
    if (key === "deal_value_usd") return usd.format(v);
    return v;
  }

  function renderReading(data) {
    const req = data.requirement;
    const asked = askedField(req, data.follow_up_question);
    provenance.replaceChildren();
    if (data.extractor === "claude") {
      provenance.append("Read by ", el("strong", null, "Claude"), ", an AI model, from the post you entered.");
    } else {
      provenance.append("Read by the ", el("strong", null, "regex baseline"), ", a fixed rule-based extractor kept for comparison. It is less accurate than the Claude extractor.");
    }
    const keys = req.is_deal_request
      ? ["is_deal_request", "state", "city", "property_type", "transaction_type", "deal_value_usd", "timeline", "notes"]
      : ["is_deal_request"];
    facts.replaceChildren();
    for (const key of keys) {
      const value = factValue(key, req);
      if (key === "notes" && value === null) continue;
      const row = el("div", "facts__row");
      const dd = el("dd");
      if (value === null) {
        row.classList.add(key === asked ? "is-asked" : "is-missing");
        dd.textContent = key === asked ? "Not in the post: asked below" : "Not in the post";
      } else {
        dd.textContent = value;
      }
      row.append(el("dt", null, FIELD_LABELS[key]), dd);
      facts.append(row);
    }
    reading.hidden = false;
  }

  // ---- Rendering results --------------------------------------------------------------------

  // The matcher's reasons name schema values ("specializes in single_family"); show the words.
  const TOKENS = {
    single_family: "single-family", condo_townhome: "condo/townhome", mixed_use: "mixed-use",
    "1031_exchange": "1031 exchange", within_30_days: "within 30 days", "31_to_90_days": "31 to 90 days",
    over_90_days: "over 90 days",
  };
  function humanize(text) {
    return text.replace(/\b(single_family|condo_townhome|mixed_use|1031_exchange|within_30_days|31_to_90_days|over_90_days)\b/g, (t) => TOKENS[t]);
  }

  function describeSource(source) {
    if (source === "fictional") return { text: "Fictional demo record, not a real licensee", fictional: true };
    const m = /^trec_(\d{4})-(\d{2})-(\d{2})$/.exec(source);
    if (m) {
      const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
      return { text: `Texas Real Estate Commission, public record as of ${longDate.format(d)}`, trec: true };
    }
    return { text: source };
  }

  function renderBroker(match, index) {
    const node = tplBroker.content.firstElementChild.cloneNode(true);
    node.querySelector(".broker__rank").textContent = String(index + 1);
    node.querySelector(".broker__name").textContent = match.name;
    node.querySelector(".broker__license").textContent = match.license_number;
    node.querySelector(".broker__type").textContent = match.license_type;

    const src = describeSource(match.source);
    const srcCell = node.querySelector(".broker__source");
    srcCell.textContent = src.text;
    if (src.fictional) srcCell.classList.add("source--fictional");
    if (src.trec) {
      const verify = el("a", null, "Verify on TREC");
      verify.href = "https://www.trec.texas.gov/apps/license-holder-search/";
      verify.target = "_blank";
      verify.rel = "noopener noreferrer";
      srcCell.append(" · ", verify);
    }

    const copy = node.querySelector(".broker__copy");
    copy.setAttribute("aria-label", `Copy license number ${match.license_number}`);
    copy.addEventListener("click", () => copyText(match.license_number));

    const reasons = node.querySelector(".reasons");
    const notes = node.querySelector(".notes");
    for (const r of match.reasons) {
      const m = /^([+-]\d+)\s+(.+)$/.exec(r);
      if (m) {
        const li = el("li", "reason");
        li.append(el("span", "reason__pts", m[1]), el("span", "reason__text", humanize(m[2])));
        reasons.append(li);
      } else {
        notes.append(el("li", "note", r));
      }
    }
    if (!reasons.children.length) {
      reasons.append(el("li", "reasons__empty", "Licensed here, but no fit points: we have no specialty, market or deal-size data that matches this post."));
    }

    const score = Math.max(0, Math.min(MAX_FIT, match.score));
    node.querySelector(".fit__num").textContent = String(match.score);
    node.querySelector(".fit").setAttribute("aria-label", `${match.score} of ${MAX_FIT} fit points`);
    node.querySelectorAll(".fit__bar span").forEach((s, i) => s.classList.toggle("is-on", i < score));
    return node;
  }

  function renderFollowUp(container, req, question) {
    const asked = askedField(req, question);
    const box = el("div", "callout");
    const eyebrow = asked === "state" ? "One question before we can match" : "One detail would sharpen these matches";
    box.append(el("p", "callout__eyebrow", eyebrow), el("p", "callout__question", question));
    const f = el("form", "answer");
    f.noValidate = true;
    const id = "answer-input";
    const label = el("label", null, "Your answer");
    label.htmlFor = id;
    const input = el("input");
    input.id = id;
    input.type = "text";
    input.autocomplete = "off";
    input.maxLength = 200;
    const go = el("button", "button button--primary", "Add to post and run again");
    go.type = "submit";
    f.append(label, input, go);
    const hint = el("p", "status-text", "Your answer is added to the end of the post, so you can see exactly what was sent.");
    f.addEventListener("submit", (e) => {
      e.preventDefault();
      const answer = input.value.trim();
      if (!answer) { input.focus(); input.setAttribute("aria-invalid", "true"); return; }
      const lead = (asked && ANSWER_LEAD[asked]) || ((a) => a);
      const next = `${textarea.value.replace(/\s+$/, "")}\n${lead(answer)}`;
      if (next.length > MAX_CHARS) {
        showError({ title: "This post is too long", body: `Adding the answer would take the post over ${num.format(MAX_CHARS)} characters. Shorten the post first.`, fieldMessage: "Shorten the post." });
        return;
      }
      textarea.value = next;
      updateCount();
      run(next);
    });
    box.append(f, hint);
    container.append(box);
  }

  function renderResults(data) {
    const req = data.requirement;
    const body = document.createDocumentFragment();
    const matches = data.matches;
    const stateName = req.state ? (STATES[req.state] || req.state) : null;

    if (!req.is_deal_request) {
      resultsHeading.textContent = "This doesn't read as a deal request";
      const n = el("div", "notice");
      n.append(
        el("p", null, "It looks like a job post, an ad or a general question rather than a client who needs a broker, so no one was matched and nothing was guessed."),
        el("p", null, "If it is a deal, say what the client wants to do (buy, sell, lease or a 1031 exchange) and where the property is."),
      );
      body.append(n);
    } else if (!req.state) {
      resultsHeading.textContent = "We need the state first";
      const n = el("div", "notice");
      n.append(el("p", null, "Licenses are issued by state, so we don't show anyone until we know where the property is. We never guess the state from a city name: several states share city names."));
      body.append(n);
      if (data.follow_up_question) renderFollowUp(body, req, data.follow_up_question);
    } else {
      resultsHeading.textContent = matches.length
        ? `${matches.length === 1 ? "1 licensed broker" : `${matches.length} licensed brokers`} in ${stateName}`
        : `No licensed brokers in ${stateName}`;

      const summary = el("div", "results-summary");
      const gate = el("p", "gate");
      const excluded = data.excluded_for_licensing;
      gate.append(
        el("strong", "gate__count", num.format(excluded)),
        ` ${excluded === 1 ? "broker" : "brokers"} in our data ${excluded === 1 ? "was" : "were"} left out because they aren't actively licensed in ${stateName}. Licensing is checked before any ranking.`,
      );
      summary.append(gate);
      if (matches.length >= 5) {
        summary.append(el("p", "results-summary__line", "Showing the top 5 by fit. Ties keep a fixed order."));
      }
      body.append(summary);

      if (matches.length) {
        const list = el("ol", "broker-list");
        list.setAttribute("aria-label", `Licensed brokers in ${stateName}, ranked by fit`);
        matches.forEach((m, i) => list.append(renderBroker(m, i)));
        body.append(list);
        // The brokers are the answer; a question about a missing detail is a refinement, so it follows them.
        if (data.follow_up_question) renderFollowUp(body, req, data.follow_up_question);
      } else {
        if (data.follow_up_question) renderFollowUp(body, req, data.follow_up_question);
        const n = el("div", "notice");
        n.append(el("p", null, `No broker in this prototype's data holds an active, unexpired license in ${stateName}. The data covers Texas public records and a small set of fictional brokers, so this says nothing about who is licensed there in reality.`));
        body.append(n);
      }
    }
    resultsBody.replaceChildren(body);
  }

  // ---- Copy ----------------------------------------------------------------------------------

  let toastTimer = null;
  function toast(message) {
    let t = document.querySelector(".toast");
    if (!t) {
      t = el("div", "toast");
      t.setAttribute("role", "status");
      document.body.append(t);
    }
    t.textContent = message;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.remove(), 3200);
  }

  async function copyText(text) {
    try {
      if (!navigator.clipboard || !window.isSecureContext) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(text);
      toast(`Copied ${text}`);
    } catch {
      // Clipboard permission denied or unavailable (e.g. plain http on a LAN address).
      toast(`Couldn't copy. Select ${text} and copy it yourself.`);
    }
  }

  // ---- Request -------------------------------------------------------------------------------

  async function run(raw) {
    const text = raw.trim();
    hideError();
    if (!text) {
      showError({ title: "Enter a deal post", body: "Paste or type the referral post you want to match.", fieldMessage: "Enter a deal post." });
      return;
    }
    if (text.length > MAX_CHARS) {
      showError({ title: "This post is too long", body: `Shorten it to ${num.format(MAX_CHARS)} characters or fewer.`, fieldMessage: "Shorten the post." });
      return;
    }
    if (!navigator.onLine) {
      showError({ title: "You're offline", body: "Reconnect to the internet and try again. Your post is still here.", retry: true });
      return;
    }
    if (inFlight) inFlight.abort();
    const controller = new AbortController();
    inFlight = controller;
    const timer = setTimeout(() => controller.abort("timeout"), REQUEST_TIMEOUT_MS);
    setLoading(true);
    try {
      const res = await fetch("/match", {
        method: "POST",
        headers: { "content-type": "application/json", accept: "application/json" },
        body: JSON.stringify({ text }),
        signal: controller.signal,
      });
      let data = null;
      try { data = await res.json(); } catch { data = null; }
      if (controller !== inFlight) return; // a newer request replaced this one
      setLoading(false);
      if (!res.ok || !data || !data.requirement) {
        restoreIntroIfEmpty();
        showError(describeFailure(res.status, data && data.detail));
        return;
      }
      lastGood = data;
      renderReading(data);
      renderResults(data);
      $("examples-wrap").open = false;
      resultsHeading.focus({ preventScroll: false });
    } catch (err) {
      if (controller !== inFlight) return;
      setLoading(false);
      restoreIntroIfEmpty();
      if (controller.signal.aborted && controller.signal.reason === "timeout") {
        showError({ title: "This is taking too long", body: "The server didn't answer within 45 seconds. Your post is still here; try again.", retry: true });
      } else if (!navigator.onLine) {
        showError({ title: "You're offline", body: "Reconnect to the internet and try again. Your post is still here.", retry: true });
      } else {
        showError({ title: "Can't reach the server", body: "The page couldn't connect to the matching service. Check that the server is running, then try again. Your post is still here.", retry: true });
      }
    } finally {
      clearTimeout(timer);
      if (controller === inFlight) inFlight = null;
    }
  }

  function restoreIntroIfEmpty() {
    // After a failed request, don't leave the loading placeholder on screen. If an earlier run
    // succeeded, show its results again, marked as not matching the latest attempt.
    if (!lastGood) {
      resultsHeading.textContent = "How matching works";
      resultsBody.replaceChildren(introBody.cloneNode(true));
      return;
    }
    renderResults(lastGood);
    const stale = el("p", "stale", "These are the results of your previous successful run. The latest request failed, so they may not match the post as it is now.");
    resultsBody.prepend(stale);
  }

  function resetAll() {
    if (inFlight) inFlight.abort();
    inFlight = null;
    setLoading(false);
    textarea.value = "";
    updateCount();
    hideError();
    reading.hidden = true;
    facts.replaceChildren();
    lastGood = null;
    resultsHeading.textContent = "How matching works";
    resultsBody.replaceChildren(introBody.cloneNode(true));
    textarea.focus();
  }

  // ---- Wiring --------------------------------------------------------------------------------

  const list = $("examples");
  for (const ex of EXAMPLES) {
    const li = el("li");
    const b = el("button", "example");
    b.type = "button";
    b.append(el("span", "example__tag", ex.tag), el("span", "example__text", ex.text));
    b.addEventListener("click", () => {
      textarea.value = ex.text;
      updateCount();
      run(ex.text);
    });
    li.append(b);
    list.append(li);
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); run(textarea.value); });
  textarea.addEventListener("input", () => { updateCount(); if (field.classList.contains("has-error")) setFieldError(null); });
  textarea.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); run(textarea.value); }
  });
  clearBtn.addEventListener("click", resetAll);
  submit.addEventListener("click", (e) => {
    if (submit.getAttribute("aria-disabled") === "true") e.preventDefault();
  });
  window.addEventListener("online", () => {
    if (!errorBox.hidden && errorTitle.textContent === "You're offline") {
      errorTitle.textContent = "You're back online";
      errorBody.textContent = "Run the post again when you're ready.";
    }
  });
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  const shortcut = document.querySelector(".actions__shortcut kbd");
  if (shortcut && !isMac) shortcut.textContent = "Ctrl";
  updateCount();
})();
