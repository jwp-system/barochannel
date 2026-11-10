// 업무 화면용 SDK. 화면은 이 객체로만 바로채널과 외부 시스템을 부른다.
//
//   Baro.context                         사용자, 채널, 명령어, 상세 파라미터(params), 권한
//   await Baro.api("GET", "search", { query: { q: "견적" } })   → { ok, status, data }
//   await Baro.api("POST", "", { body: {...}, idempotent: true })
//   Baro.message({ text, fields, detail, link })   남길 메시지를 정한다. [남기고 닫기]가 켜진다
//   Baro.message(null)                   남길 것이 없다. [남기고 닫기]가 꺼진다
//   await Baro.people("이영")             바로채널 사용자 찾기 → [{ id, name, email }]
//   Baro.status("문구", { error: true })   아래쪽 상태 문구
//   Baro.close()                         아무것도 남기지 않고 닫는다
//
// 외부 응답 값은 textContent로만 화면에 넣는다. innerHTML을 쓰지 않는다.
(() => {
  "use strict";
  const context = JSON.parse(document.getElementById("baro-context").textContent);
  const inFrame = window.parent !== window;
  let pending = null;
  let sending = false;
  let dirty = false;

  function tell(type, extra) {
    if (inFrame) window.parent.postMessage({ type, ...extra }, location.origin);
  }

  function status(text, options) {
    const box = document.querySelector("[data-baro-status]");
    if (!box) return;
    box.textContent = text || "";
    box.classList.toggle("error", Boolean(options && options.error));
  }

  async function request(url, init) {
    try {
      const response = await fetch(url, { credentials: "same-origin", ...init });
      const text = await response.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch { data = { error: "응답을 읽지 못했습니다." }; }
      return { ok: response.ok, status: response.status, data };
    } catch {
      return { ok: false, status: 0, data: { error: "바로채널에 연결하지 못했습니다." } };
    }
  }

  function api(method, path, options) {
    const opts = options || {};
    const query = new URLSearchParams();
    Object.entries(opts.query || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") query.append(key, String(value));
    });
    const headers = { Accept: "application/json" };
    if (method !== "GET") headers["X-CSRF-Token"] = context.csrf;
    if (opts.idempotent) headers["Idempotency-Key"] = typeof opts.idempotent === "string" ? opts.idempotent : context.key;
    let body;
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }
    const clean = String(path || "").replace(/^\/+/, "");
    const url = context.api + clean + (query.toString() ? `?${query}` : "");
    return request(url, { method, headers, body });
  }

  function errorText(result, fallback) {
    const data = result.data || {};
    return data.error || (typeof data.detail === "string" ? data.detail : "") || fallback;
  }

  function leaveButton() {
    return document.querySelector("[data-baro-leave]");
  }

  function message(value) {
    pending = value || null;
    const button = leaveButton();
    if (button) button.disabled = !pending || sending;
  }

  async function leave() {
    if (!pending || sending) return;
    sending = true;
    const button = leaveButton();
    button.disabled = true;
    status("남기는 중입니다…");
    const body = { ...pending };
    if (context.reply_to && body.reply_to === undefined) body.reply_to = context.reply_to;
    const result = await request(context.messages, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": context.csrf,
        "Idempotency-Key": context.key,
      },
      body: JSON.stringify(body),
    });
    sending = false;
    if (result.ok) {
      status("남겼습니다.");
      dirty = false;
      tell("baro:posted", { post_id: result.data.post_id });
      return;
    }
    button.disabled = false;
    status(errorText(result, "남기지 못했습니다."), { error: true });
  }

  function close() {
    tell("baro:close");
  }

  async function people(q) {
    const word = String(q || "").trim();
    if (!word) return [];
    const result = await request(`${context.people}?q=${encodeURIComponent(word)}`);
    return result.ok ? result.data : [];
  }

  document.addEventListener("input", () => {
    if (dirty) return;
    dirty = true;
    tell("baro:dirty", { dirty: true });
  });
  document.addEventListener("keydown", (event) => {
    // Esc는 바깥 팝업에 맡긴다. 입력 중이면 바깥에서 닫을지 묻는다.
    if (event.key === "Escape") { event.preventDefault(); tell("baro:escape"); }
  });
  document.addEventListener("DOMContentLoaded", () => {
    const closer = document.querySelector("[data-baro-close]");
    if (closer) closer.addEventListener("click", close);
    const button = leaveButton();
    if (button) button.addEventListener("click", leave);
  });

  window.Baro = Object.freeze({ context, api, message, people, status, close, errorText });
})();
