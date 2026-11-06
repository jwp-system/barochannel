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
          catchUp(this.url);
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
  document.addEventListener("DOMContentLoaded", scrollTimeline);
  scrollTimeline();
})();
