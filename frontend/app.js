"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const storageKey = "resolve.visitor.v1";
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const state = {
    identity: null, session: null, config: null, generation: 0,
    messages: new Map(), events: new Map(), messageCursor: 0, eventCursor: 0,
    pollTimer: null, openingTimer: null, paymentTimer: null, returnTimer: null,
    bankTimer: null, toastTimer: null, requests: new Set(), connected: false,
    checkoutActive: false, checkoutSending: false, operation: "checkout",
    pendingBank: null, chatBusy: false, replayBusy: false, pendingChat: null,
    source: null, automaticPaused: false, renderedCount: 0
  };

  function icon(name, className = "icon") {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", className);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function persist(identity) {
    try {
      if (identity) sessionStorage.setItem(storageKey, JSON.stringify(identity));
      else sessionStorage.removeItem(storageKey);
    } catch (_) { /* The current page still works when browser storage is disabled. */ }
  }

  function savedIdentity() {
    try {
      const value = JSON.parse(sessionStorage.getItem(storageKey));
      return value && typeof value.id === "string" && typeof value.token === "string" ? value : null;
    } catch (_) { return null; }
  }

  async function api(path, {method = "GET", body, authenticated = true} = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 65000);
    state.requests.add(controller);
    const headers = {Accept: "application/json"};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (authenticated && state.identity) headers.Authorization = `Bearer ${state.identity.token}`;
    try {
      const response = await fetch(path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal, cache: "no-store"
      });
      const data = response.status === 204 ? null : await response.json();
      if (!response.ok) {
        const error = new Error(typeof data?.detail === "string" ? data.detail : "The request could not be completed.");
        error.status = response.status;
        throw error;
      }
      return data;
    } finally {
      clearTimeout(timeout);
      state.requests.delete(controller);
    }
  }

  const sessionPath = (suffix = "") => `/api/sessions/${state.identity.id}${suffix}`;
  function toast(text) {
    clearTimeout(state.toastTimer);
    $("toast").textContent = text;
    $("toast").hidden = false;
    state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 6500);
  }

  function connection(text = "") {
    $("connection-banner").textContent = text;
    $("connection-banner").hidden = !text;
  }

  function errorText(error) {
    return error.name === "AbortError" || error instanceof TypeError
      ? "The backend could not be reached. Keep FastAPI running and try again."
      : error.message;
  }

  function stopTimers() {
    ["pollTimer", "openingTimer", "paymentTimer", "returnTimer", "bankTimer"].forEach((key) => {
      clearTimeout(state[key]); state[key] = null;
    });
    $("demo-cursor").hidden = true;
    $("demo-cursor").getAnimations().forEach((animation) => animation.cancel());
  }

  function nearBottom() {
    const thread = $("thread");
    return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 130;
  }

  function scrollBottom() {
    $("thread").scrollTop = $("thread").scrollHeight;
    $("new-messages").hidden = true;
  }

  function timeLabel(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "now" : date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  }

  function productCard() {
    const card = element("div", "product-card");
    const image = element("img");
    image.src = "/assets/shirt.svg";
    image.alt = "Sage green Everyday cotton shirt, illustrated";
    image.width = 640; image.height = 440;
    const copy = element("div", "product-copy");
    copy.append(element("span", "product-eyebrow", "THE EVERYDAY EDIT"));
    copy.append(element("h3", "", "Everyday cotton shirt"));
    copy.append(element("p", "", "Soft cotton. Easy fit. Made for everyday."));
    const price = element("div", "product-price");
    price.append(element("strong", "", "₹1,000"), element("span", "", "Sage green · Quantity 1"));
    const pay = element("button", "product-pay", "Pay ₹1,000");
    pay.id = "product-pay"; pay.type = "button";
    pay.append(icon("arrow"));
    pay.addEventListener("click", () => openCheckout("checkout"));
    copy.append(price, pay, element("span", "product-note", "Simulated purchase · No money will move"));
    card.append(image, copy);
    return card;
  }

  function messageNode(message) {
    const row = element("div", `message-row ${message.role === "user" ? "user" : ""}`);
    row.dataset.messageId = message.id;
    const bubble = element("div", "message-bubble");
    if (message.role !== "user") {
      const sender = element("div", `message-sender ${message.role === "assistant" ? "resolve" : ""}`,
        message.role === "assistant" ? "Resolve" : "Thread & Loom");
      if (message.role === "assistant") sender.prepend(icon("shield"));
      bubble.append(sender);
    }
    bubble.append(element("p", "message-text", message.text));
    const meta = element("div", "message-meta");
    if (message.source === "gemini") meta.append(element("span", "message-source", "Gemini interpretation"));
    else if (message.source.startsWith("fallback")) meta.append(element("span", "message-source", "Demo fallback"));
    meta.append(element("span", "", timeLabel(message.created_at)));
    if (message.role === "user") meta.append(element("span", "read-ticks", "✓✓"));
    bubble.append(meta); row.append(bubble);
    if (message.id === 1 && message.role === "merchant") row.append(productCard());
    return row;
  }

  function renderMessages(forceScroll = false) {
    // Keep the narrative in the conversation after returning from checkout.
    if (state.checkoutActive) return;
    const sticky = forceScroll || nearBottom();
    const previousTop = $("thread").scrollTop;
    const sorted = [...state.messages.values()].sort((a, b) => a.id - b.id);
    if (sorted.length === state.renderedCount) return;
    const fragment = document.createDocumentFragment();
    sorted.forEach((message) => fragment.append(messageNode(message)));
    $("messages").replaceChildren(fragment);
    const hadMessages = state.renderedCount > 0;
    state.renderedCount = sorted.length;
    const last = sorted.at(-1);
    if (last) { $("contact-preview").textContent = last.text; $("contact-time").textContent = timeLabel(last.created_at); }
    drawStatus();
    if (sticky) requestAnimationFrame(scrollBottom);
    else { $("thread").scrollTop = previousTop; $("new-messages").hidden = !hadMessages; }
  }

  function applySnapshot(snapshot, {historical = false, forceScroll = false} = {}) {
    if (!snapshot || snapshot.session_id !== state.identity?.id) return;
    if (!state.session || snapshot.version >= state.session.version) state.session = snapshot;
    snapshot.messages.forEach((message) => state.messages.set(message.id, message));
    snapshot.events.forEach((event) => {
      if (!state.events.has(event.id)) {
        state.events.set(event.id, event);
        if (!historical) processEvent(event);
      }
    });
    state.messageCursor = Math.max(state.messageCursor, snapshot.last_message_id);
    state.eventCursor = Math.max(state.eventCursor, snapshot.last_event_id);
    renderMessages(forceScroll);
    drawStatus();
  }

  function modeText() {
    if (state.source === "gemini") return "Gemini connected · replies grounded in this case";
    if (state.source === "controller_credentials") return "Resolve · credential-sharing protection";
    if (state.source?.startsWith("fallback")) return state.source === "fallback_no_key"
      ? "Demo fallback · Gemini key not configured" : "Demo fallback · Gemini unavailable for this reply";
    return state.config?.chat_mode === "gemini_configured" ? "Gemini configured · awaiting first chat" : "Demo fallback · Gemini key not configured";
  }

  function statusCopy(s) {
    if (!state.connected) return ["Connection interrupted", "Showing the last known case. Reconnecting to check for updates.", "warning", "clock"];
    if (!s.evidence_available) return ["Latest evidence unavailable", "Payment confirmation and retry are held until evidence returns.", "warning", "clock"];
    if (s.order.status === "expired") return ["Order expired", "Another payment is blocked. Your payment concern can be prepared for support.", "warning", "shield"];
    if (s.order.status === "paid") {
      if (s.phase === "refund_pending") return ["Order paid · earlier refund initiated", "Bank credit for the earlier payment has not been verified.", "confirmed", "check"];
      if (s.phase === "refund_processed") return ["Order paid · earlier refund processed", "Gateway record updated. The bank-credit time is not verified.", "confirmed", "check"];
      if (s.phase === "paid_tracking_earlier") return ["Order paid through another attempt", "No further payment needed. The earlier attempt is being tracked.", "confirmed", "check"];
      return ["Payment received. Order confirmed.", "₹1,000 captured · your merchant has confirmed the order.", "confirmed", "check"];
    }
    if (s.case_status === "support_pending") return ["Your case is prepared for support", "Confirmation is unresolved. This demo has not contacted a real team.", "warning", "shield"];
    if (s.phase === "authorized") return ["Bank authorisation received", "Capture and merchant confirmation are pending. Please don’t pay again.", "", "clock"];
    if (s.phase === "captured") return ["Payment captured", "Waiting for the merchant to confirm the order. No further payment needed.", "", "clock"];
    if (s.phase === "actionable_failure") return ["Insufficient funds · order unpaid", "An eligible retry is available through the approved checkout.", "warning", "shield"];
    if (s.phase === "processing") return ["Payment processing", "Waiting for the bank response. Please don’t make another payment.", "", "clock"];
    return ["Payment confirmation pending", "Resolve is tracking the bank response. Please wait before paying again.", "", "clock"];
  }

  function drawStatus() {
    const s = state.session;
    if (!s) return;
    const canChat = state.connected && s.consent && s.case_status !== "not_open" && !state.chatBusy && !state.replayBusy;
    $("message-input").disabled = !canChat;
    $("send-button").disabled = !canChat || !$("message-input").value.trim();
    $("message-input").placeholder = !s.consent ? "Buyer communication is turned off" : s.case_status === "not_open"
      ? "Resolve will join after your payment attempt…" : "Message Resolve about your payment…";
    $("suggestions").hidden = !canChat || !s.evidence_available || s.order.status === "paid";
    $("typing-indicator").hidden = !state.chatBusy;
    $("replay-button").disabled = state.replayBusy;
    $("scenario-replay").disabled = state.replayBusy;
    $("evidence-toggle").checked = s.evidence_available;
    $("consent-toggle").checked = s.consent;
    $("chat-mode").textContent = modeText();
    $("mode-detail").textContent = modeText() + ". Gemini interprets intent and language; the backend controls payment facts.";
    $("header-subtitle").textContent = s.order.status === "paid" ? "Order confirmed · Resolve is here if you need it" : s.case_status !== "not_open" ? "Resolve is following your payment" : "Business account · demo";
    const pay = $("product-pay");
    if (pay) {
      pay.disabled = !state.connected || !s.allowed_actions.includes("checkout") || state.checkoutSending;
      pay.replaceChildren(document.createTextNode(s.phase === "shopping" ? "Pay ₹1,000" : s.order.status === "paid" ? "Order confirmed" : "Payment attempt started"), icon(s.order.status === "paid" ? "check" : "arrow"));
    }
    $("case-card").hidden = s.phase === "shopping" || state.checkoutActive;
    if (s.phase !== "shopping") {
      const [title, description, appearance, mark] = statusCopy(s);
      $("case-title").textContent = title;
      $("case-description").textContent = description;
      $("case-card").className = "case-card" + (appearance ? ` is-${appearance}` : "");
      $("case-icon").replaceChildren(icon(mark));
    }
    $("receipt-button").hidden = !s.receipt || !state.connected;
    $("retry-button").hidden = !s.allowed_actions.includes("retry") || !state.connected;
    $("support-button").hidden = !s.allowed_actions.includes("support") || !state.connected || s.case_status === "support_pending";
    $("experience-hint").textContent = s.phase === "shopping" ? "Watch the opening play out, or tap Pay to begin." : !s.consent
      ? "Communication is off. Payment status remains available; enable communication in demo controls to chat again."
      : s.order.status === "paid" ? "One payment story, followed through to closure. Open your receipt or replay another scenario."
      : s.case_status === "support_pending" ? "The payment is still unresolved. The support handoff above is simulated."
      : "Try asking Resolve a question. Payment updates arrive independently of the conversation.";
  }

  function processEvent(event) {
    if (event.type === "payment_failed" && state.checkoutActive) showCheckoutFailure();
    if (event.type === "order_confirmed" && state.checkoutActive) {
      const generation = state.generation;
      clearTimeout(state.returnTimer);
      state.returnTimer = setTimeout(() => { if (generation === state.generation) $("checkout-dialog").close(); }, 500);
    }
    if (event.type === "bank_debit_alert") {
      state.pendingBank = event;
      if (!state.checkoutActive) showBankAlert();
    }
  }

  function showBankAlert() {
    if (!state.pendingBank) return;
    $("bank-alert").querySelector("h3").textContent = "₹1,000 debited";
    $("bank-alert").querySelector("p").textContent = state.session?.order.status === "paid"
      ? "This earlier debit alert was followed by order confirmation."
      : "Debit alert received. Payment confirmation is still pending.";
    $("bank-alert").hidden = false;
    state.pendingBank = null;
    clearTimeout(state.bankTimer);
    state.bankTimer = setTimeout(() => { $("bank-alert").hidden = true; }, 8500);
  }

  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if (!state.identity) return;
    state.pollTimer = setTimeout(poll, state.config?.poll_interval_ms || 1500);
  }

  async function poll() {
    const generation = state.generation;
    if (!state.identity) return;
    try {
      const snapshot = await api(sessionPath(`?after_message_id=${state.messageCursor}&after_event_id=${state.eventCursor}`));
      if (generation !== state.generation) return;
      state.connected = true; connection(); applySnapshot(snapshot);
    } catch (error) {
      if (generation !== state.generation) return;
      state.connected = false;
      if ([401, 403, 404, 410].includes(error.status)) {
        persist(null); state.identity = null;
        connection("This demo session has ended. Choose Replay to start a new one.");
      } else connection("Connection interrupted. Showing the last known status; retry and chat are held while reconnecting.");
      drawStatus();
    } finally { if (generation === state.generation) schedulePoll(); }
  }

  async function animatedClick(button, generation) {
    if (!button || button.disabled || generation !== state.generation) return;
    button.scrollIntoView({block: "nearest", behavior: reducedMotion ? "instant" : "smooth"});
    await new Promise((resolve) => setTimeout(resolve, reducedMotion ? 20 : 260));
    if (generation !== state.generation || button.disabled || !button.isConnected) return;
    const rect = button.getBoundingClientRect();
    const cursor = $("demo-cursor");
    cursor.hidden = reducedMotion;
    const targetX = rect.left + rect.width * .58;
    const targetY = rect.top + rect.height * .5;
    if (!reducedMotion) {
      const animation = cursor.animate([
        {transform: `translate(${targetX + 100}px, ${targetY + 65}px)`, opacity: 0},
        {transform: `translate(${targetX}px, ${targetY}px)`, opacity: 1}
      ], {duration: 650, easing: "cubic-bezier(.25,.8,.25,1)", fill: "forwards"});
      try { await animation.finished; } catch (_) { return; }
    }
    if (generation !== state.generation || button.disabled || !button.isConnected) { cursor.hidden = true; return; }
    cursor.classList.add("clicking"); button.classList.add("demo-click");
    await new Promise((resolve) => setTimeout(resolve, 170));
    if (generation === state.generation && !button.disabled) button.click();
    button.classList.remove("demo-click");
    cursor.classList.remove("clicking"); cursor.hidden = true;
    cursor.getAnimations().forEach((animation) => animation.cancel());
  }

  function pauseOpening() {
    clearTimeout(state.openingTimer); state.openingTimer = null;
    state.automaticPaused = true;
    $("demo-cursor").getAnimations().forEach((animation) => animation.cancel());
    $("demo-cursor").hidden = true;
    if (state.session?.phase === "shopping") {
      $("auto-banner").hidden = false;
      $("auto-label").textContent = "Opening paused. Tap Pay whenever you’re ready.";
      $("pause-button").hidden = true;
    }
  }

  function scheduleOpening() {
    if (state.session?.phase !== "shopping") return;
    state.automaticPaused = false;
    $("auto-banner").hidden = false; $("pause-button").hidden = false;
    $("auto-label").textContent = "Watch the opening: checkout begins automatically…";
    const generation = state.generation;
    state.openingTimer = setTimeout(() => animatedClick($("product-pay"), generation), 2800);
  }

  function openCheckout(operation) {
    if (!state.session || !state.connected || state.checkoutActive) return;
    if (!state.session.allowed_actions.includes(operation)) { toast("This payment action is held for the current case."); return; }
    clearTimeout(state.openingTimer);
    $("auto-banner").hidden = true;
    state.operation = operation; state.checkoutActive = true; state.checkoutSending = false;
    $("checkout-title").textContent = operation === "retry" ? "Retry your payment" : "Complete your payment";
    $("checkout-options").hidden = false; $("checkout-progress").hidden = true; $("checkout-failure").hidden = true;
    $("checkout-pay").disabled = false; $("checkout-close").disabled = false;
    $("checkout-dialog").showModal(); drawStatus();
    const generation = state.generation;
    state.paymentTimer = setTimeout(() => {
      if (generation === state.generation && $("checkout-dialog").open) animatedClick($("checkout-pay"), generation);
    }, 1700);
  }

  async function submitCheckout() {
    if (state.checkoutSending || !state.checkoutActive) return;
    clearTimeout(state.paymentTimer); state.checkoutSending = true;
    $("checkout-pay").disabled = true;
    $("checkout-options").hidden = true; $("checkout-progress").hidden = false;
    const generation = state.generation;
    try {
      const snapshot = await api(sessionPath(`/${state.operation}`), {method: "POST", body: {request_id: crypto.randomUUID(), method: "upi"}});
      if (generation !== state.generation) return;
      applySnapshot(snapshot); clearTimeout(state.pollTimer);
      state.pollTimer = setTimeout(poll, 400);
    } catch (error) {
      if (generation !== state.generation) return;
      $("checkout-dialog").close(); toast(errorText(error));
      await poll();
    }
  }

  function showCheckoutFailure() {
    $("checkout-progress").hidden = true; $("checkout-options").hidden = true; $("checkout-failure").hidden = false;
    const failure = [...state.session.payments].find((payment) => payment.error_reason);
    $("checkout-failure").querySelector("h3").textContent = failure?.error_reason === "insufficient_funds" ? "Payment failed" : "Payment could not be confirmed";
    $("checkout-error").textContent = failure?.error_reason === "insufficient_funds" ? "Insufficient funds in the selected account." : "The bank response timed out.";
    const generation = state.generation;
    clearTimeout(state.returnTimer);
    state.returnTimer = setTimeout(() => { if (generation === state.generation) $("checkout-dialog").close(); }, 1250);
  }

  $("checkout-dialog").addEventListener("close", () => {
    clearTimeout(state.paymentTimer); state.checkoutActive = false; state.checkoutSending = false;
    renderMessages(true); drawStatus();
    if (state.pendingBank) showBankAlert();
    if (state.session?.phase === "shopping") pauseOpening();
  });

  async function sendMessage(text) {
    text = text.trim();
    if (!text || state.chatBusy || !state.connected || !state.session?.consent || state.session.case_status === "not_open") return;
    const generation = state.generation;
    const request = state.pendingChat?.text === text ? state.pendingChat : {request_id: crypto.randomUUID(), text};
    state.pendingChat = request; state.chatBusy = true; drawStatus(); scrollBottom();
    try {
      const response = await api(sessionPath("/messages"), {method: "POST", body: request});
      if (generation !== state.generation) return;
      state.source = response.chat_source; state.pendingChat = null;
      $("message-input").value = ""; $("message-input").style.height = "";
      applySnapshot(response.session, {forceScroll: true});
    } catch (error) {
      if (generation !== state.generation) return;
      toast(errorText(error) + " Send the same message again to retry.");
      $("message-input").value = text;
    } finally {
      if (generation === state.generation) {
        state.chatBusy = false; drawStatus();
        if (!$("message-input").disabled) $("message-input").focus({preventScroll: true});
      }
    }
  }

  async function changeSetting(suffix, body) {
    if (!state.identity) return;
    const generation = state.generation;
    try {
      const snapshot = await api(sessionPath(suffix), {method: "PATCH", body});
      if (generation === state.generation) applySnapshot(snapshot);
    } catch (error) { if (generation === state.generation) { toast(errorText(error)); drawStatus(); } }
  }

  async function requestSupport() {
    if (!state.identity) return;
    const generation = state.generation;
    $("support-button").disabled = true;
    try {
      const snapshot = await api(sessionPath("/support"), {method: "POST"});
      if (generation === state.generation) applySnapshot(snapshot, {forceScroll: true});
    } catch (error) { if (generation === state.generation) toast(errorText(error)); }
    finally { $("support-button").disabled = false; }
  }

  function openReceipt() {
    const receipt = state.session?.receipt;
    if (!receipt || !state.connected) return;
    const details = $("receipt-details"); details.replaceChildren();
    [["Order", receipt.order_id], ["Payment", receipt.payment_id], ["Method", "UPI · simulated"], ["Status", "Captured · merchant confirmed"]].forEach(([label, value]) => {
      details.append(element("dt", "", label), element("dd", "", value));
    });
    $("receipt-dialog").showModal();
  }

  function setIdentity(created) {
    state.identity = {id: created.session.session_id, token: created.session_token};
    persist(state.identity); state.session = null;
    state.messages.clear(); state.events.clear(); state.messageCursor = 0; state.eventCursor = 0; state.renderedCount = 0;
    state.pendingBank = null; state.pendingChat = null; state.source = null; state.chatBusy = false;
    $("messages").replaceChildren(); $("bank-alert").hidden = true; $("new-messages").hidden = true;
    $("message-input").value = ""; $("message-input").style.height = "";
    $("scenario-select").value = created.session.scenario;
    $("language-select").value = created.session.language;
    state.connected = true; connection();
    applySnapshot(created.session, {historical: true, forceScroll: true});
  }

  async function replay() {
    if (state.replayBusy) return;
    state.replayBusy = true; state.generation++; stopTimers();
    state.requests.forEach((controller) => controller.abort());
    ["controls-dialog", "receipt-dialog", "checkout-dialog"].forEach((id) => { if ($(id).open) $(id).close(); });
    const generation = state.generation;
    const body = {scenario: $("scenario-select").value, language: $("language-select").value};
    drawStatus();
    try {
      let created;
      if (state.identity) {
        try { created = await api(sessionPath("/reset"), {method: "POST", body}); }
        catch (error) {
          if (![401, 403, 404, 410].includes(error.status)) throw error;
          state.identity = null; persist(null);
        }
      }
      if (!created) created = await api("/api/sessions", {method: "POST", body, authenticated: false});
      if (generation !== state.generation) return;
      setIdentity(created); scheduleOpening(); schedulePoll();
    } catch (error) { if (generation === state.generation) { toast(errorText(error)); schedulePoll(); } }
    finally { if (generation === state.generation) { state.replayBusy = false; state.chatBusy = false; drawStatus(); } }
  }

  async function initialize() {
    try {
      state.config = await api("/api/config", {authenticated: false});
      state.identity = savedIdentity();
      if (state.identity) {
        try {
          const snapshot = await api(sessionPath());
          state.connected = true; applySnapshot(snapshot, {historical: true, forceScroll: true});
          $("scenario-select").value = snapshot.scenario; $("language-select").value = snapshot.language;
        } catch (error) {
          if (![401, 403, 404, 410].includes(error.status)) throw error;
          state.identity = null; persist(null);
        }
      }
      if (!state.identity) {
        const created = await api("/api/sessions", {method: "POST", body: {scenario: "timeout_success", language: "en"}, authenticated: false});
        setIdentity(created);
      }
      scheduleOpening(); schedulePoll(); drawStatus();
    } catch (error) {
      connection(errorText(error) + " Open this page through the running FastAPI server, then choose Replay.");
      $("experience-hint").textContent = "The conversation needs the FastAPI backend to begin.";
      $("replay-button").disabled = false;
    }
  }

  $("chat-form").addEventListener("submit", (event) => { event.preventDefault(); sendMessage($("message-input").value); });
  $("message-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendMessage(event.target.value); }
  });
  $("message-input").addEventListener("input", () => {
    $("message-input").style.height = "auto";
    $("message-input").style.height = `${Math.min(110, $("message-input").scrollHeight)}px`;
    drawStatus();
  });
  document.querySelectorAll("[data-question]").forEach((button) => button.addEventListener("click", () => sendMessage(button.dataset.question)));
  document.querySelectorAll("[data-action='controls']").forEach((button) => button.addEventListener("click", () => { pauseOpening(); drawStatus(); $("controls-dialog").showModal(); }));
  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $(button.dataset.close).close()));
  $("replay-button").addEventListener("click", replay); $("scenario-replay").addEventListener("click", replay);
  $("pause-button").addEventListener("click", pauseOpening);
  $("start-button").addEventListener("click", () => { clearTimeout(state.openingTimer); state.automaticPaused = false; animatedClick($("product-pay"), state.generation); });
  $("checkout-pay").addEventListener("click", submitCheckout);
  $("checkout-close").addEventListener("click", () => $("checkout-dialog").close());
  $("dismiss-bank").addEventListener("click", () => { $("bank-alert").hidden = true; });
  $("receipt-button").addEventListener("click", openReceipt);
  $("retry-button").addEventListener("click", () => openCheckout("retry"));
  $("support-button").addEventListener("click", requestSupport);
  $("evidence-toggle").addEventListener("change", (event) => changeSetting("/demo/evidence", {available: event.target.checked}));
  $("consent-toggle").addEventListener("change", (event) => changeSetting("/consent", {allowed: event.target.checked}));
  $("new-messages").addEventListener("click", scrollBottom);
  $("thread").addEventListener("scroll", () => { if (nearBottom()) $("new-messages").hidden = true; });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.identity) { clearTimeout(state.pollTimer); poll(); } });
  window.addEventListener("beforeunload", () => { stopTimers(); state.requests.forEach((controller) => controller.abort()); });
  initialize();
})();
