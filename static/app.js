document.querySelectorAll("[data-confirm]").forEach((button) => {
  button.addEventListener("click", (event) => {
    const message = button.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });
});

window.setTimeout(() => {
  document.querySelectorAll(".flash").forEach((item) => {
    item.style.opacity = "0";
    item.style.transform = "translateY(-6px)";
    item.style.transition = "opacity 0.25s ease, transform 0.25s ease";
    window.setTimeout(() => item.remove(), 260);
  });
}, 3500);

async function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.style.position = "absolute";
  field.style.left = "-9999px";
  document.body.appendChild(field);
  field.select();
  document.execCommand("copy");
  document.body.removeChild(field);
}

document.querySelectorAll("[data-copy-text]").forEach((button) => {
  const originalLabel = button.textContent;
  const successLabel = button.getAttribute("data-copy-success") || "已复制";
  button.addEventListener("click", async () => {
    const text = button.getAttribute("data-copy-text");
    if (!text) {
      return;
    }
    try {
      await copyText(text);
      button.textContent = successLabel;
      window.setTimeout(() => {
        button.textContent = originalLabel;
      }, 1600);
    } catch (_error) {
      button.textContent = "复制失败";
      window.setTimeout(() => {
        button.textContent = originalLabel;
      }, 1600);
    }
  });
});
