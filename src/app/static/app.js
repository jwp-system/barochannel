(function () {
  const toast = document.getElementById("toast");
  let timer;

  function showToast(text) {
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(timer);
    timer = setTimeout(() => { toast.hidden = true; }, 4000);
  }

  function errorText(xhr) {
    try {
      const detail = JSON.parse(xhr.responseText).detail;
      return typeof detail === "string" ? detail : "입력값을 확인해 주세요.";
    } catch {
      return "요청을 처리하지 못했습니다.";
    }
  }

  function scrollTimeline() {
    const box = document.getElementById("timeline-scroll");
    if (box) box.scrollTop = box.scrollHeight;
  }

  // 브라우저는 호스트마다 연결을 6개만 연다. 탭마다 SSE를 붙들고 있으면 7번째 탭이 멈추므로
  // 보이는 탭만 연결하고, 다시 보이면 놓친 내용을 받아 온다.
  function catchUp(url) {
    const channelId = url.split("/").pop();
    // 문서·결재 보기 화면에는 타임라인과 고정 글이 없고 쓰레드만 있다.
    if (document.getElementById("timeline") && !document.querySelector("#timeline form.card")) {
      htmx.ajax("GET", `/channels/${channelId}/posts`, { target: "#timeline", swap: "innerHTML" });
    }
    if (document.getElementById("pinned")) {
      htmx.ajax("GET", `/channels/${channelId}/pins`, { target: "#pinned", swap: "innerHTML" });
    }
    const list = document.getElementById("thread-list");
    const draft = document.querySelector("#thread textarea");
    if (list && !list.querySelector("form.card") && !(draft && draft.value)) {
      const postId = list.getAttribute("sse-swap").split("-")[1];
      htmx.ajax("GET", `/posts/${postId}/thread`, { target: "#thread", swap: "innerHTML" });
    }
  }

  class VisibleEventSource {
    constructor(url) {
      this.url = url;
      this.listeners = [];
      this.real = null;
      this.closed = false;
      this.missed = false;
      this.sync = () => this.update();
      document.addEventListener("visibilitychange", this.sync);
      this.update();
    }

    get readyState() {
      if (this.closed) return EventSource.CLOSED;
      return this.real ? this.real.readyState : EventSource.CONNECTING;
    }

    update() {
      const visible = document.visibilityState !== "hidden";
      if (visible && !this.real && !this.closed) {
        this.open();
      } else if (!visible && this.real) {
        this.real.close();
        this.real = null;
        this.missed = true;
      }
    }

    open() {
      const real = new EventSource(this.url, { withCredentials: true });
      this.real = real;
      real.onopen = (event) => {
        if (this.onopen) this.onopen(event);
        if (this.missed) {
          this.missed = false;
          // 개수 연결은 접속하면 서버가 바로 현재 값을 보낸다.
          if (!this.url.endsWith("/me")) catchUp(this.url);
        }
      };
      real.onerror = (event) => {
        // 끊긴 연결은 확장이 새 객체로 다시 만든다. 이 객체가 또 열리지 않게 닫아 둔다.
        if (real.readyState === EventSource.CLOSED) this.close();
        if (this.onerror) this.onerror(event);
      };
      this.listeners.forEach(([name, listener]) => real.addEventListener(name, listener));
    }

    addEventListener(name, listener) {
      this.listeners.push([name, listener]);
      if (this.real) this.real.addEventListener(name, listener);
    }

    removeEventListener(name, listener) {
      this.listeners = this.listeners.filter((item) => item[0] !== name || item[1] !== listener);
      if (this.real) this.real.removeEventListener(name, listener);
    }

    close() {
      this.closed = true;
      document.removeEventListener("visibilitychange", this.sync);
      if (this.real) this.real.close();
      this.real = null;
    }
  }

  htmx.createEventSource = (url) => new VisibleEventSource(url);

  // 다시 연결하는 사이에 같은 글이 두 번 들어오지 않게 한다.
  document.body.addEventListener("htmx:sseBeforeMessage", (event) => {
    const found = /id="post-(\d+)"/.exec(event.detail.data);
    const appends = /^(ch|thread)-/.test(event.detail.type);
    if (appends && found && document.getElementById(`post-${found[1]}`)) event.preventDefault();
  });

  // 작성 응답이 SSE보다 늦게 오면 같은 글을 또 넣지 않는다.
  document.body.addEventListener("htmx:beforeSwap", (event) => {
    const target = event.detail.target;
    if (event.detail.requestConfig.verb !== "post") return;
    if (!target || !["timeline", "thread-list"].includes(target.id)) return;
    const body = event.detail.serverResponse || "";
    const found = /^\s*<article id="post-(\d+)"/.exec(body);
    if (found && document.getElementById(`post-${found[1]}`)) event.detail.shouldSwap = false;
  });

  document.body.addEventListener("htmx:responseError", (event) => showToast(errorText(event.detail.xhr)));
  document.body.addEventListener("htmx:sendError", () => showToast("서버에 연결하지 못했습니다."));

  document.body.addEventListener("htmx:afterRequest", (event) => {
    const form = event.detail.elt;
    if (event.detail.successful && form.matches && form.matches("form.js-reset")) {
      form.reset();
      const preview = form.querySelector(".preview");
      if (preview) { preview.hidden = true; preview.textContent = ""; }
      const names = form.querySelector(".file-names");
      if (names) names.textContent = "";
      scrollTimeline();
    }
  });

  document.body.addEventListener("htmx:afterSwap", (event) => {
    if (!event.detail.target || event.detail.target.id !== "thread") return;
    document.body.classList.add("thread-open");
    // 답글 주소(#post-번호)로 들어오면 쓰레드에서 그 답글을 보여 준다.
    const reply = location.hash.startsWith("#post-") && document.querySelector(`#thread ${location.hash}`);
    if (reply) reply.scrollIntoView({ block: "center" });
  });

  // 문서·결재 보기 화면은 넓은 화면에서 쓰레드를 바로 연다. 좁은 화면은 쓰레드가 본문을 덮으므로
  // 답글 주소로 들어왔을 때만 연다.
  const autoThread = document.querySelector("[data-auto-thread]");
  if (autoThread && (window.matchMedia("(min-width: 801px)").matches || location.hash.startsWith("#post-"))) {
    htmx.ajax("GET", autoThread.dataset.autoThread, { target: "#thread", swap: "innerHTML" });
  }

  // 쓰레드 창 너비. 손잡이를 끌거나 화살표 키로 바꾸고, 이 브라우저에 기억한다.
  const WIDTH_KEY = "baro.threadWidth";
  const resizers = document.querySelectorAll(".thread-resizer");

  function clampWidth(width) {
    const max = Math.max(320, Math.min(window.innerWidth - 480, 960));
    return Math.round(Math.min(Math.max(width, 300), max));
  }

  function setThreadWidth(width, save) {
    const value = clampWidth(width);
    document.documentElement.style.setProperty("--thread-width", `${value}px`);
    resizers.forEach((handle) => {
      handle.setAttribute("aria-valuenow", String(value));
      handle.setAttribute("aria-valuemin", "300");
      handle.setAttribute("aria-valuemax", String(clampWidth(Infinity)));
    });
    if (save) {
      try { localStorage.setItem(WIDTH_KEY, String(value)); } catch { /* 저장소를 못 써도 이번 화면에는 적용된다. */ }
    }
    return value;
  }

  function currentWidth() {
    const pane = document.getElementById("thread");
    return pane ? pane.getBoundingClientRect().width : 380;
  }

  try {
    const saved = Number.parseInt(localStorage.getItem(WIDTH_KEY) || "", 10);
    if (saved) setThreadWidth(saved, false);
  } catch { /* 기본 너비를 쓴다. */ }

  resizers.forEach((handle) => {
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      const pane = document.getElementById("thread");
      const right = pane.getBoundingClientRect().right;
      handle.setPointerCapture(event.pointerId);
      document.body.classList.add("thread-resizing");
      const move = (moveEvent) => setThreadWidth(right - moveEvent.clientX, false);
      const stop = (upEvent) => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", stop);
        handle.removeEventListener("pointercancel", stop);
        document.body.classList.remove("thread-resizing");
        setThreadWidth(right - upEvent.clientX, true);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", stop);
      handle.addEventListener("pointercancel", stop);
    });
    handle.addEventListener("keydown", (event) => {
      const steps = { ArrowLeft: 24, ArrowRight: -24, Home: -10000, End: 10000 };
      if (!(event.key in steps)) return;
      event.preventDefault();
      setThreadWidth(currentWidth() + steps[event.key], true);
    });
    // 두 번 누르면 기본 너비로 돌아간다.
    handle.addEventListener("dblclick", () => setThreadWidth(380, true));
  });

  window.addEventListener("resize", () => {
    const value = getComputedStyle(document.documentElement).getPropertyValue("--thread-width");
    if (value) setThreadWidth(Number.parseInt(value, 10), false);
  });

  document.body.addEventListener("click", (event) => {
    if (event.target.matches(".js-close-thread")) {
      document.getElementById("thread").replaceChildren();
      document.body.classList.remove("thread-open");
    }
  });

  document.body.addEventListener("change", (event) => {
    if (!event.target.matches('.file-button input')) return;
    const names = event.target.closest("form").querySelector(".file-names");
    names.textContent = Array.from(event.target.files, (file) => file.name).join(", ");
  });

  function csrfHeaders() {
    try {
      return JSON.parse(document.body.getAttribute("hx-headers") || "{}");
    } catch {
      return {};
    }
  }

  // 지금 보고 있는 채널에 새 글이 오면 바로 읽은 것으로 한다. 짧게 모아 한 번만 보낸다.
  let readTimer;
  function readCurrentChannel(channelId) {
    clearTimeout(readTimer);
    readTimer = setTimeout(() => {
      if (document.visibilityState === "hidden") return;
      fetch(`/channels/${channelId}/read`, { method: "POST", headers: csrfHeaders() });
    }, 400);
  }

  function showUnread(data) {
    const current = document.querySelector(".sidebar a[aria-current][data-channel]");
    const currentId = current ? current.dataset.channel : null;
    document.querySelectorAll("[data-unread]").forEach((badge) => {
      const id = badge.dataset.unread;
      if (!(id in data.channels)) return;
      // 보고 있는 채널은 곧 읽음 처리되므로 숫자를 띄우지 않는다.
      const count = id === currentId ? 0 : data.channels[id];
      badge.querySelector(".unread-number").textContent = count > 99 ? "99+" : String(count);
      badge.hidden = count === 0;
      const link = badge.closest("a[data-channel]");
      if (link) link.classList.toggle("has-unread", count > 0);
    });
    document.querySelectorAll("[data-unread-group]").forEach((dot) => {
      const id = dot.dataset.unreadGroup;
      if (id in data.groups) dot.hidden = data.groups[id] === 0;
    });
    if (currentId && data.channels[currentId] > 0) readCurrentChannel(currentId);
  }

  function setupTodoCount() {
    const badges = document.querySelectorAll(".js-todo-count");
    if (!badges.length) return;
    const source = new VisibleEventSource("/events/me");
    source.addEventListener("counts", (event) => {
      const count = Number(event.data);
      badges.forEach((badge) => {
        badge.textContent = count > 99 ? "99+" : String(count);
        badge.hidden = count === 0;
      });
    });
    source.addEventListener("unread", (event) => {
      try {
        showUnread(JSON.parse(event.data));
      } catch { /* 형식이 맞지 않으면 다음 신호를 기다린다. */ }
    });
  }

  // F-20: 입력 중인 @단어를 찾아 후보를 보여준다. 후보 문구는 textContent로만 넣는다.
  function setupMentions() {
    let menu = null;
    let field = null;
    let start = 0;
    let serial = 0;
    let waiting;

    function close() {
      if (menu) menu.remove();
      menu = null;
    }

    function choose(button) {
      const caret = field.selectionStart;
      const text = `${button.dataset.insert} `;
      field.setRangeText(text, start, caret, "end");
      field.focus();
      close();
    }

    function move(step) {
      const items = Array.from(menu.querySelectorAll("button:not(:disabled)"));
      if (!items.length) return;
      const index = items.findIndex((item) => item.classList.contains("on"));
      items.forEach((item) => item.classList.remove("on"));
      items[(index + step + items.length) % items.length].classList.add("on");
    }

    function show(rows) {
      close();
      if (!rows.length) return;
      menu = document.createElement("div");
      menu.className = "mention-menu";
      rows.forEach((row) => {
        const button = document.createElement("button");
        button.type = "button";
        button.disabled = row.disabled;
        button.dataset.insert = row.insert;
        const detail = document.createElement("small");
        detail.textContent = row.disabled ? "읽기 권한 없음" : row.detail;
        button.append(row.label, detail);
        menu.append(button);
      });
      menu.addEventListener("mousedown", (event) => {
        const button = event.target.closest("button");
        event.preventDefault();
        if (button && !button.disabled) choose(button);
      });
      field.parentElement.append(menu);
      move(1);
    }

    async function load(url, query, current) {
      const response = await fetch(`${url}?q=${encodeURIComponent(query)}`);
      if (response.ok && current === serial) show(await response.json());
    }

    document.body.addEventListener("input", (event) => {
      if (!event.target.matches("textarea[data-mentions]")) return;
      field = event.target;
      const typed = field.value.slice(0, field.selectionStart);
      const found = /(?:^|[\s(])@([^\s@]*)$/.exec(typed);
      serial += 1;
      clearTimeout(waiting);
      close();
      if (!found) return;
      start = typed.length - found[1].length;
      const current = serial;
      waiting = setTimeout(() => load(field.dataset.mentions, found[1], current), 150);
    });

    document.body.addEventListener("keydown", (event) => {
      if (!menu || event.target !== field) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        move(event.key === "ArrowDown" ? 1 : -1);
      } else if ((event.key === "Enter" || event.key === "Tab") && !event.isComposing) {
        const selected = menu.querySelector("button.on");
        if (selected) { event.preventDefault(); choose(selected); }
      } else if (event.key === "Escape") {
        close();
      }
    });

    document.body.addEventListener("focusout", (event) => {
      if (event.target === field) close();
    });
  }

  // 승인선과 검토자가 함께 쓰는 사람 검색. 후보 문구는 textContent로만 넣는다.
  function personText(row) {
    const name = document.createElement("span");
    name.textContent = row.name;
    const detail = document.createElement("small");
    detail.textContent = [row.department, row.email].filter(Boolean).join(" · ");
    return [name, detail];
  }

  function iconButton(text, label, disabled, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    button.setAttribute("aria-label", label);
    button.disabled = disabled;
    button.addEventListener("click", action);
    return button;
  }

  function spacer() {
    const item = document.createElement("span");
    item.className = "spacer";
    return item;
  }

  function attachPeopleSearch(search, url, chosen, onPick) {
    const input = search.querySelector("input");
    const people = new WeakMap();
    let menu = null;
    let serial = 0;
    let waiting;

    function close() {
      if (menu) menu.remove();
      menu = null;
    }

    function add(button) {
      onPick(people.get(button));
      input.value = "";
      close();
      input.focus();
    }

    function move(step) {
      const items = Array.from(menu.querySelectorAll("button:not(:disabled)"));
      if (!items.length) return;
      const index = items.findIndex((item) => item.classList.contains("on"));
      items.forEach((item) => item.classList.remove("on"));
      items[(index + step + items.length) % items.length].classList.add("on");
    }

    function show(rows) {
      close();
      if (!rows.length) return;
      menu = document.createElement("div");
      menu.className = "mention-menu";
      rows.forEach((row) => {
        const button = document.createElement("button");
        const taken = chosen().some((item) => item.email === row.email);
        const [name, detail] = personText(row);
        button.type = "button";
        button.disabled = taken || Boolean(row.blocked);
        if (taken) detail.textContent = "이미 추가됨";
        else if (row.blocked) detail.textContent = row.blocked;
        button.append(name, detail);
        people.set(button, row);
        menu.append(button);
      });
      menu.addEventListener("mousedown", (event) => {
        const button = event.target.closest("button");
        event.preventDefault();
        if (button && !button.disabled) add(button);
      });
      search.append(menu);
      move(1);
    }

    async function find() {
      serial += 1;
      const current = serial;
      const response = await fetch(`${url}?q=${encodeURIComponent(input.value.trim())}`);
      if (response.ok && current === serial) show(await response.json());
    }

    input.addEventListener("input", () => {
      // 이전 검색 결과가 남아 있으면 빠른 Enter가 엉뚱한 사람을 고른다.
      serial += 1;
      close();
      clearTimeout(waiting);
      waiting = setTimeout(find, 150);
    });
    input.addEventListener("focus", find);
    input.addEventListener("blur", () => { serial += 1; close(); });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.isComposing) {
        // 검색 칸의 Enter가 폼 제출로 이어지면 안 된다.
        event.preventDefault();
        const selected = menu && menu.querySelector("button.on");
        if (selected) add(selected);
      } else if (menu && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
        event.preventDefault();
        move(event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Escape") {
        close();
      }
    });
  }

  // 승인선은 단계 목록으로 편집한다. 저장할 때는 숨은 칸의 이메일 목록을 보낸다.
  function setupLinePicker() {
    const form = document.querySelector(".line-picker");
    if (!form) return;
    const lines = JSON.parse(form.dataset.lines);
    const select = form.elements.department_id;
    const list = form.querySelector(".line-steps");
    const empty = form.querySelector(".line-empty");
    let approvers = [];

    function swap(index, other) {
      [approvers[index], approvers[other]] = [approvers[other], approvers[index]];
      render();
    }

    function render() {
      list.replaceChildren(...approvers.map((row, index) => {
        const item = document.createElement("li");
        const last = index === approvers.length - 1;
        const step = document.createElement("b");
        step.textContent = last ? "최종" : `${index + 1}차`;
        item.append(
          step, ...personText(row), spacer(),
          iconButton("▲", `${row.name} 위로`, index === 0, () => swap(index, index - 1)),
          iconButton("▼", `${row.name} 아래로`, last, () => swap(index, index + 1)),
          iconButton("빼기", `${row.name} 빼기`, false, () => { approvers.splice(index, 1); render(); }),
        );
        return item;
      }));
      empty.hidden = approvers.length > 0;
      form.elements.approvers.value = approvers.map((row) => row.email).join("\n");
    }

    function load() {
      approvers = (lines[select.value] || []).map((row) => ({ ...row }));
      render();
    }

    attachPeopleSearch(form.querySelector(".line-search"), form.dataset.candidates,
      () => approvers, (row) => { approvers.push(row); render(); });
    select.addEventListener("change", load);
    document.querySelectorAll(".js-edit-line").forEach((button) => {
      button.addEventListener("click", () => {
        select.value = button.dataset.department;
        load();
        form.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    });
    load();
  }

  // 결재선·검토자·참조자 편집기. 결재선은 순서가 있고 마지막이 최종 승인자다.
  // 한 사람은 한 역할만 맡으므로 다른 목록에 있는 사람은 검색에서 "이미 추가됨"으로 막는다.
  function setupApprovalPlanner(planner) {
    const form = planner.closest("form");
    const data = JSON.parse(planner.dataset.plan);
    const lists = { line: data.line, reviewers: data.reviewers, references: data.references };
    const names = { line: "line_ids", reviewers: "reviewer_ids", references: "reference_ids" };
    const inputs = planner.querySelector(".planner-inputs");
    const note = planner.querySelector(".planner-note");
    const select = form && form.elements.department_id;
    const everyone = () => [...lists.line, ...lists.reviewers, ...lists.references];

    function stepLabel(index, total) {
      return index === total - 1 ? "최종" : `${index + 1}차`;
    }

    function render(role) {
      const group = planner.querySelector(`[data-role="${role}"]`);
      const rows = lists[role];
      group.querySelector(".line-steps").replaceChildren(...rows.map((row, index) => {
        const item = document.createElement("li");
        const label = document.createElement("b");
        label.textContent = role === "line" ? stepLabel(index, rows.length) : (role === "reviewers" ? "검토" : "참조");
        const remove = () => { rows.splice(index, 1); changed(role); };
        const controls = role === "line"
          ? [
            iconButton("▲", `${row.name} 위로`, index === 0, () => swap(index, index - 1)),
            iconButton("▼", `${row.name} 아래로`, index === rows.length - 1, () => swap(index, index + 1)),
            iconButton("빼기", `${row.name} 빼기`, false, remove),
          ]
          : [iconButton("빼기", `${row.name} 빼기`, false, remove)];
        item.append(label, ...personText(row), spacer(), ...controls);
        return item;
      }));
      const empty = group.querySelector(".line-empty");
      if (empty) empty.hidden = rows.length > 0;
    }

    function sync() {
      inputs.replaceChildren(...Object.entries(lists).flatMap(([role, rows]) => rows.map((row) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = names[role];
        input.value = row.id;
        return input;
      })));
    }

    function changed(role, message = "") {
      render(role);
      sync();
      if (role === "line") note.textContent = message;
    }

    function swap(index, other) {
      const rows = lists.line;
      [rows[index], rows[other]] = [rows[other], rows[index]];
      changed("line");
    }

    async function loadDefault(message) {
      const query = select && select.value ? `?department_id=${encodeURIComponent(select.value)}` : "";
      const response = await fetch(`${planner.dataset.defaultLine}${query}`);
      if (!response.ok) return;
      const rows = await response.json();
      // 기본 결재선의 사람이 검토자·참조자에 있으면 결재선 쪽을 남긴다.
      const ids = new Set(rows.map((row) => row.id));
      lists.reviewers = lists.reviewers.filter((row) => !ids.has(row.id));
      lists.references = lists.references.filter((row) => !ids.has(row.id));
      lists.line = rows;
      ["reviewers", "references"].forEach(render);
      changed("line", message);
    }

    Object.keys(lists).forEach((role) => {
      const group = planner.querySelector(`[data-role="${role}"]`);
      attachPeopleSearch(group.querySelector(".line-search"), planner.dataset.people, everyone,
        (row) => { lists[role].push(row); changed(role); });
      render(role);
    });
    sync();
    planner.querySelector(".js-reset-line").addEventListener("click", () => loadDefault("등록 승인선으로 되돌렸습니다."));
    if (select) select.addEventListener("change", () => loadDefault("기안 소속을 바꿔 그 소속의 등록 승인선으로 다시 채웠습니다."));
  }

  document.addEventListener("DOMContentLoaded", setupTodoCount);
  document.addEventListener("DOMContentLoaded", setupMentions);
  document.addEventListener("DOMContentLoaded", setupLinePicker);

  // 작성 화면의 작성·미리보기 탭. 미리보기는 저장할 때와 같은 서버 정제를 거친다.
  function showEditorTab(editor, name) {
    const field = editor.querySelector("textarea");
    const pane = editor.querySelector(".editor-preview");
    if (name === "preview") pane.style.minHeight = `${field.offsetHeight}px`;
    editor.querySelectorAll("[data-editor-tab]").forEach((tab) => {
      const on = tab.dataset.editorTab === name;
      tab.classList.toggle("on", on);
      tab.setAttribute("aria-selected", String(on));
    });
    field.hidden = name !== "write";
    pane.hidden = name !== "preview";
    if (name === "write") field.focus();
  }

  document.body.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-editor-tab]");
    if (!tab) return;
    const editor = tab.closest(".editor");
    const name = tab.dataset.editorTab;
    showEditorTab(editor, name);
    if (name === "preview") renderPreview(editor);
  });

  // 폼 안에서 htmx로 보내면 폼의 다른 칸까지 섞여 가므로 본문만 따로 보낸다.
  let previewSerial = 0;

  async function renderPreview(editor) {
    const pane = editor.querySelector(".editor-preview");
    // 이전 미리보기가 남아 보이거나 늦게 온 응답이 최신 내용을 덮지 않게 한다.
    const current = ++previewSerial;
    pane.replaceChildren();
    pane.setAttribute("aria-busy", "true");
    const headers = JSON.parse(document.body.getAttribute("hx-headers") || "{}");
    const response = await fetch("/preview", {
      method: "POST",
      headers: { ...headers, "HX-Request": "true" },
      body: new URLSearchParams({ body: editor.querySelector("textarea").value }),
    });
    const html = await response.text();
    if (current !== previewSerial) return;
    pane.removeAttribute("aria-busy");
    if (!response.ok) { showToast(errorText({ responseText: html })); return; }
    // 서버가 마크다운을 정제한 조각이다. 저장할 때와 같은 규칙을 쓴다.
    pane.innerHTML = html;
  }

  // 미리보기 중에 제출하면 숨은 필수 칸을 브라우저가 알려주지 못한다. 작성 탭으로 돌려 보여준다.
  document.addEventListener("invalid", (event) => {
    const editor = event.target.closest && event.target.closest(".editor");
    if (editor && event.target.hidden) showEditorTab(editor, "write");
  }, true);
  document.body.addEventListener("click", (event) => {
    if (!event.target.matches(".js-add-option")) return;
    const box = event.target.closest(".poll-options");
    const count = box.querySelectorAll("input[name=options]").length;
    if (count >= 20) return;
    const input = document.createElement("input");
    input.name = "options";
    input.maxLength = 200;
    input.placeholder = `선택지 ${count + 1}`;
    input.setAttribute("aria-label", input.placeholder);
    event.target.before(input);
    input.focus();
  });
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".approval-planner").forEach(setupApprovalPlanner);
  });
  // 업무 채널. 명령어나 상세 보기는 업무 화면(templates/works)을 팝업 안의 iframe으로 연다.
  // 화면은 postMessage로 닫기(baro:close), 남김(baro:posted), 입력 중(baro:dirty)을 알린다.
  const workModal = document.getElementById("work-modal");
  let workFrame = null;
  let workDirty = false;

  function openWorkFrame(url, size, title) {
    const body = document.getElementById("work-modal-body");
    workFrame = document.createElement("iframe");
    workFrame.className = "work-frame";
    workFrame.src = url;
    workFrame.title = title || "업무 화면";
    body.replaceChildren(workFrame);
    workModal.dataset.size = size || "medium";
    workDirty = false;
    if (!workModal.open) workModal.showModal();
    workFrame.focus();
  }

  function closeWorkModal(force) {
    if (!force && workDirty && !confirm("입력한 내용이 사라집니다. 닫을까요?")) return;
    workModal.close();
    document.getElementById("work-modal-body").replaceChildren();
    workFrame = null;
  }

  if (workModal) {
    window.addEventListener("message", (event) => {
      if (!workFrame || event.origin !== location.origin || event.source !== workFrame.contentWindow) return;
      const data = event.data || {};
      if (data.type === "baro:dirty") workDirty = Boolean(data.dirty);
      else if (data.type === "baro:close") closeWorkModal(true);
      else if (data.type === "baro:escape") closeWorkModal(false);
      else if (data.type === "baro:posted") {
        closeWorkModal(true);
        const input = document.querySelector(".work-composer input[name=command]");
        if (input) input.value = "";
        showToast("채널에 남겼습니다.");
        scrollTimeline();
      }
    });
    workModal.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeWorkModal(false);
    });
    workModal.addEventListener("click", (event) => {
      if (event.target === workModal) closeWorkModal(false);
    });
    document.addEventListener("click", (event) => {
      const link = event.target.closest("[data-work-frame]");
      if (!link || event.metaKey || event.ctrlKey || event.shiftKey) return;
      event.preventDefault();
      openWorkFrame(link.dataset.workFrame, "medium", link.dataset.workTitle);
    });
    // 상세 링크(/posts/번호#detail-글번호)를 직접 열면 상세 화면을 띄운다.
    document.addEventListener("DOMContentLoaded", () => {
      const found = /^#detail-(\d+)$/.exec(location.hash);
      const link = found && document.querySelector(`[data-work-frame="/works/messages/${found[1]}/detail"]`);
      if (link) openWorkFrame(link.dataset.workFrame, "medium", link.dataset.workTitle);
    });
  }

  function setupWorkComposer(form) {
    const input = form.querySelector("input[name=command]");
    const box = form.querySelector(".work-input");
    let commands = null;
    let menu = null;

    function close() {
      if (menu) menu.remove();
      menu = null;
    }

    async function run(name) {
      close();
      if (commands === null) {
        const response = await fetch(form.dataset.commands);
        commands = response.ok ? await response.json() : [];
      }
      const command = commands.find((row) => row.name === name.toLowerCase());
      if (!command) { showToast(`없는 명령어입니다: /${name}`); return; }
      openWorkFrame(command.url, command.size, command.label);
    }

    function move(step) {
      const items = Array.from(menu.querySelectorAll("button"));
      const index = items.findIndex((item) => item.classList.contains("on"));
      items.forEach((item) => item.classList.remove("on"));
      items[(index + step + items.length) % items.length].classList.add("on");
    }

    async function show() {
      const typed = input.value.trim();
      close();
      if (!typed.startsWith("/")) return;
      if (commands === null) {
        const response = await fetch(form.dataset.commands);
        commands = response.ok ? await response.json() : [];
      }
      const word = typed.slice(1).toLowerCase();
      const rows = commands.filter((row) => row.name.startsWith(word) || row.label.includes(word));
      if (input.value.trim() !== typed) return;
      menu = document.createElement("div");
      menu.className = "mention-menu";
      if (!rows.length) {
        const empty = document.createElement("button");
        empty.type = "button";
        empty.disabled = true;
        empty.textContent = commands.length ? "맞는 명령어가 없습니다" : "등록된 명령어가 없습니다";
        menu.append(empty);
      }
      rows.forEach((row) => {
        const button = document.createElement("button");
        const detail = document.createElement("small");
        button.type = "button";
        button.dataset.name = row.name;
        detail.textContent = row.description ? `${row.label} · ${row.description}` : row.label;
        button.append(`/${row.name}`, detail);
        menu.append(button);
      });
      menu.addEventListener("mousedown", (event) => {
        const button = event.target.closest("button[data-name]");
        event.preventDefault();
        if (button) run(button.dataset.name);
      });
      box.append(menu);
      if (rows.length) move(1);
    }

    input.addEventListener("input", show);
    input.addEventListener("focus", show);
    input.addEventListener("blur", close);
    input.addEventListener("keydown", (event) => {
      if (!menu) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (menu.querySelector("button[data-name]")) move(event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Escape") {
        close();
      } else if ((event.key === "Enter" || event.key === "Tab") && !event.isComposing) {
        const selected = menu.querySelector("button.on");
        if (selected) { event.preventDefault(); run(selected.dataset.name); }
      }
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const found = /^\/(\S+)/.exec(input.value.trim());
      if (found) run(found[1]);
      else showToast("명령어를 / 로 시작해 입력해 주세요.");
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".work-composer").forEach(setupWorkComposer);
  });
  document.addEventListener("DOMContentLoaded", scrollTimeline);
  scrollTimeline();
})();
