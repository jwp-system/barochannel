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
    if (!document.querySelector("#timeline form.card")) {
      htmx.ajax("GET", `/channels/${channelId}/posts`, { target: "#timeline", swap: "innerHTML" });
    }
    htmx.ajax("GET", `/channels/${channelId}/pins`, { target: "#pinned", swap: "innerHTML" });
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
    if (event.detail.target && event.detail.target.id === "thread") document.body.classList.add("thread-open");
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

  function setupTodoCount() {
    const badges = document.querySelectorAll(".js-todo-count");
    if (!badges.length) return;
    new VisibleEventSource("/events/me").addEventListener("counts", (event) => {
      const count = Number(event.data);
      badges.forEach((badge) => {
        badge.textContent = count > 99 ? "99+" : String(count);
        badge.hidden = count === 0;
      });
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

  // 검토자는 순서가 없다. 고른 사람마다 숨은 칸을 하나씩 둔다.
  function setupReviewerPicker() {
    const form = document.querySelector(".reviewer-picker");
    if (!form) return;
    const list = form.querySelector(".line-steps");
    const inputs = form.querySelector(".reviewer-inputs");
    let reviewers = [];

    function render() {
      list.replaceChildren(...reviewers.map((row, index) => {
        const item = document.createElement("li");
        const step = document.createElement("b");
        step.textContent = "검토";
        item.append(step, ...personText(row), spacer(),
          iconButton("빼기", `${row.name} 빼기`, false, () => { reviewers.splice(index, 1); render(); }));
        return item;
      }));
      inputs.replaceChildren(...reviewers.map((row) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "reviewer_ids";
        input.value = row.id;
        return input;
      }));
    }

    attachPeopleSearch(form.querySelector(".line-search"), form.dataset.candidates,
      () => reviewers, (row) => { reviewers.push(row); render(); });
  }

  function setupPin() {
    const input = document.getElementById("pin-input");
    if (!input) return;
    const form = document.getElementById("pin-form");
    const box = document.querySelector(".pin-dots");
    const dots = box.querySelectorAll("i");
    const title = document.getElementById("pin-title");
    const steps = form.dataset.steps.split(",");
    const titles = (form.dataset.titles || "").split("|");
    let step = 0;

    function render() {
      dots.forEach((dot, index) => dot.classList.toggle("on", index < input.value.length));
      if (title && titles[step]) title.replaceChildren(titles[step], document.createElement("br"), "입력해 주세요");
    }

    function restart() {
      step = 0;
      input.value = "";
      steps.forEach((name) => { form.elements[name].value = ""; });
      render();
    }

    function update(value) {
      input.value = value.replace(/\D/g, "").slice(0, 6);
      render();
      if (input.value.length < 6) return;
      form.elements[steps[step]].value = input.value;
      if (step === steps.length - 1) {
        htmx.trigger(form, "submit");
        return;
      }
      step += 1;
      input.value = "";
      render();
    }

    input.addEventListener("input", () => update(input.value));
    document.querySelector(".keypad").addEventListener("click", (event) => {
      const key = event.target.dataset.key;
      if (!key) return;
      update(key === "back" ? input.value.slice(0, -1) : input.value + key);
      input.focus();
    });
    form.addEventListener("htmx:afterRequest", (event) => {
      if (event.detail.xhr.getResponseHeader("HX-Redirect")) return;
      restart();
      box.classList.remove("shake");
      void box.offsetWidth;
      box.classList.add("shake");
    });
  }

  document.addEventListener("DOMContentLoaded", setupPin);
  document.addEventListener("DOMContentLoaded", setupTodoCount);
  document.addEventListener("DOMContentLoaded", setupMentions);
  document.addEventListener("DOMContentLoaded", setupLinePicker);
  document.addEventListener("DOMContentLoaded", setupReviewerPicker);
  document.addEventListener("DOMContentLoaded", scrollTimeline);
  scrollTimeline();
})();
