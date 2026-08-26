// marker_platform 前端交互脚本
// 布局：左侧选择栏（PDF文件转换 / 论文信息提取 / 针对材料信息提取），右侧对应工作区

/* ==================== 工作区切换 ==================== */
const navBtns = document.querySelectorAll(".nav-btn");
navBtns.forEach((btn) => {
  btn.addEventListener("click", () => {
    navBtns.forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".workspace").forEach((ws) => {
      ws.classList.toggle("is-hidden", ws.id !== btn.dataset.ws);
    });
    if (btn.dataset.ws === "ws-knowledge") void loadKnowledgeState(true);
  });
});

/* ==================== 工作区 1：PDF 文件转换 ==================== */
const form = document.getElementById("convert-form");
const fileInput = document.getElementById("file-input");
const chooseBtn = document.getElementById("choose-btn");
const dropzone = document.getElementById("dropzone");
const fileListEl = document.getElementById("file-list");
const status = document.getElementById("status");
const progressEl = document.getElementById("progress");
const btn = document.getElementById("submit-btn");
const runsEl = document.getElementById("runs");

// 已选文件（File 对象数组）
let files = [];
// 保留本次页面会话中的原始 PDF；加入材料工作区时需重新提交它构建 FigureIndex。
const sourcePdfFiles = new Map();
// 本次会话内上传过的 PDF 文件名（供论文信息提取工作区引用）
const uploadedPdfNames = new Set();
// 所有运行的结果（runs[i] = 第 i+1 次运行的 results 数组）；仅刷新页面时重置
const runs = [];

// 转换进行中的状态（用于「停止转换」交互）
let isConverting = false;       // 是否正在转换（hover 时按钮变红显示「停止转换」）
let stopConvertFlag = false;     // 用户已点停止，前端尽快退出轮询
let currentConvertTaskId = null;
let convertRunToken = 0;

// ---------- 上传列表：渲染 + 逐个删除 ----------
function renderFileList() {
  fileListEl.innerHTML = "";
  files.forEach((f, idx) => {
    const li = document.createElement("li");
    li.className = "file-item";

    const name = document.createElement("span");
    name.className = "fname";
    const kb = (f.size / 1024).toFixed(1);
    name.textContent = `${f.name}  ·  ${kb} KB`;

    const del = document.createElement("button");
    del.type = "button";
    del.className = "del-btn";
    del.textContent = "×";
    del.title = "删除该文件";
    del.addEventListener("click", () => {
      files.splice(idx, 1);   // 只删这一个
      renderFileList();
      updateStatus();
    });

    li.appendChild(name);
    li.appendChild(del);
    fileListEl.appendChild(li);
  });
}

function updateStatus() {
  status.textContent = files.length ? `已选择 ${files.length} 个文件` : "";
}

chooseBtn.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  for (const f of fileInput.files) {
    files.push(f);
    sourcePdfFiles.set(f.name, f);
    uploadedPdfNames.add(f.name);
  }
  fileInput.value = "";           // 清空，便于重复选同一文件
  renderFileList();
  updateStatus();
});

// 拖拽上传
dropzone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("drag");
});
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("drag");
  for (const f of e.dataTransfer.files) {
    if (f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf")) {
      files.push(f);
      sourcePdfFiles.set(f.name, f);
      uploadedPdfNames.add(f.name);
    }
  }
  renderFileList();
  updateStatus();
});

// ---------- 工具 ----------
function getSelectedFormats() {
  return Array.from(
    form.querySelectorAll('input[name="output_format"]:checked')
  ).map((c) => c.value);
}

function download(filename, content, mime) {
  const blob = new Blob([content], { type: mime || "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Markdown 先转换为 HTML，再由 KaTeX 处理其中的 LaTeX 文本节点。
// 未加载 KaTeX 时保留原始公式文本，确保离线预览仍可阅读。
function renderMarkdown(container, markdown) {
  const source = markdown || "";
  if (window.marked && marked.parse) container.innerHTML = marked.parse(source);
  else container.textContent = source;
  // 模型输出属于非可信内容：保留常用 Markdown/表格/图片，移除可执行节点与事件属性。
  container.querySelectorAll("script, iframe, object, embed, form, input, button, textarea, select").forEach((node) => node.remove());
  container.querySelectorAll("*").forEach((node) => {
    [...node.attributes].forEach((attribute) => {
      const name = attribute.name.toLowerCase();
      const value = String(attribute.value || "").trim().toLowerCase();
      if (name.startsWith("on") || ((name === "href" || name === "src") && value.startsWith("javascript:"))) {
        node.removeAttribute(attribute.name);
      }
    });
  });
  if (!window.renderMathInElement) return;
  try {
    renderMathInElement(container, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
      ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code", "option"],
    });
  } catch (error) {
    console.warn("KaTeX 渲染失败，已保留原始公式：", error);
  }
}

// 仅用于报告网页预览的图号引用。图片仍由后端按 JSON FigureIndex 写入
// 原始 Markdown，因此导出文件保持完整；网页只把已验证图号做成交互入口。
const REPORT_FIGURE_REF_RE = /(?:Fig(?:ure)?\.?\s*|图\s*)(\d+[a-z]?(?![a-z0-9])(?:\s*(?:,|，|、|\b(?:and|to)\b|和|及|-|–|~)\s*(?:\d+[a-z]?(?![a-z0-9])|[a-z](?![a-z0-9])))*)/gi;
const REPORT_FIGURE_TOKEN_RE = /\d+[a-z]?|[a-z](?![a-z0-9])/gi;

function figureKeysFromReportReference(refs, figureMap) {
  const keys = [];
  let currentNumber = "";
  const tokens = String(refs || "").match(REPORT_FIGURE_TOKEN_RE) || [];
  tokens.forEach((rawToken) => {
    const token = rawToken.toLowerCase();
    const numberMatch = token.match(/^(\d+)([a-z]?)$/);
    let requested = "";
    let parent = "";
    if (numberMatch) {
      currentNumber = numberMatch[1];
      requested = "fig" + token;
      parent = "fig" + currentNumber;
    } else if (currentNumber) {
      requested = "fig" + currentNumber + token;
      parent = "fig" + currentNumber;
    } else {
      return;
    }
    const key = figureMap[requested] ? requested :
      (requested !== parent && figureMap[parent] ? parent : "");
    if (key && !keys.includes(key)) keys.push(key);
  });
  return keys;
}

function figureMapForReport(reportId) {
  const paperId = String(reportId || "").split("::", 1)[0];
  const source = convertedFiles.find((paper) => paper.id === paperId);
  return figureMapFromIndex(source && source.figureIndex);
}

function figureMapFromIndex(figureIndex) {
  const figureMap = {};
  (figureIndex || []).forEach((figure) => {
    const key = String(figure.id || "").trim().toLowerCase();
    const image = String(figure.image_url || "").trim();
    if (!/^fig\d+[a-z]?$/.test(key) || !image.startsWith("/api/")) return;
    figureMap[key] = {
      id: key,
      label: figure.label || ("Fig. " + key.slice(3)),
      caption: figure.caption || "",
      image,
    };
  });
  return figureMap;
}

const MATHML_TEX_SYMBOLS = {
  "Δ": "\\Delta",
  "δ": "\\delta",
  "α": "\\alpha",
  "β": "\\beta",
  "γ": "\\gamma",
  "μ": "\\mu",
  "π": "\\pi",
  "σ": "\\sigma",
  "τ": "\\tau",
  "φ": "\\phi",
  "ω": "\\omega",
  "Ω": "\\Omega",
};

function escapeFigureCaptionTex(value) {
  return String(value || "").replace(/[\\{}#$%&_~^]/g, (character) => ({
    "\\": "\\backslash{}",
    "{": "\\{",
    "}": "\\}",
    "#": "\\#",
    "$": "\\$",
    "%": "\\%",
    "&": "\\&",
    "_": "\\_",
    "~": "\\textasciitilde{}",
    "^": "\\textasciicircum{}",
  })[character]);
}

function mathMlChildrenToTex(node) {
  return [...node.childNodes].map((child) => mathMlNodeToTex(child)).join("");
}

function mathMlElementChildren(node) {
  return [...node.children].filter((child) => child.nodeType === Node.ELEMENT_NODE);
}

function mathMlNodeToTex(node) {
  if (node.nodeType === Node.TEXT_NODE) {
    return escapeFigureCaptionTex(node.nodeValue || "").replace(/\s+/g, " ");
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return "";

  const tag = node.tagName.toLowerCase();
  const children = mathMlElementChildren(node);
  const childTex = () => mathMlChildrenToTex(node);
  const first = (index) => mathMlNodeToTex(children[index]);
  const texText = String(node.textContent || "").trim();

  if (tag === "math" || tag === "mrow" || tag === "mstyle" || tag === "mphantom" || tag === "mpadded") {
    return childTex();
  }
  if (tag === "semantics") {
    const presentation = children.find((child) => !["annotation", "annotation-xml"].includes(child.tagName.toLowerCase()));
    return presentation ? mathMlNodeToTex(presentation) : "";
  }
  if (tag === "mi") {
    const symbol = MATHML_TEX_SYMBOLS[texText];
    if (symbol) return symbol;
    const variant = String(node.getAttribute("mathvariant") || "").toLowerCase();
    return texText.length > 1 || variant === "normal"
      ? `\\mathrm{${escapeFigureCaptionTex(texText)}}`
      : escapeFigureCaptionTex(texText);
  }
  if (tag === "mn") return escapeFigureCaptionTex(texText);
  if (tag === "mtext") return `\\text{${escapeFigureCaptionTex(texText)}}`;
  if (tag === "mo") {
    const operator = {
      "−": "-",
      "–": "-",
      "×": "\\times ",
      "·": "\\cdot ",
      "≤": "\\le ",
      "≥": "\\ge ",
      "≠": "\\ne ",
      "±": "\\pm ",
      "∞": "\\infty ",
      "°": "^\\circ",
    }[texText];
    return operator || escapeFigureCaptionTex(texText);
  }
  if (tag === "msub" && children.length >= 2) return `${first(0)}_{${first(1)}}`;
  if (tag === "msup" && children.length >= 2) return `${first(0)}^{${first(1)}}`;
  if (tag === "msubsup" && children.length >= 3) return `${first(0)}_{${first(1)}}^{${first(2)}}`;
  if (tag === "mfrac" && children.length >= 2) return `\\frac{${first(0)}}{${first(1)}}`;
  if (tag === "msqrt") return `\\sqrt{${childTex()}}`;
  if (tag === "mroot" && children.length >= 2) return `\\sqrt[${first(1)}]{${first(0)}}`;
  if (tag === "mfenced") {
    const open = node.getAttribute("open") || "(";
    const close = node.getAttribute("close") || ")";
    const separator = node.getAttribute("separators") || ",";
    return `${escapeFigureCaptionTex(open)}${children.map((child) => mathMlNodeToTex(child)).join(escapeFigureCaptionTex(separator))}${escapeFigureCaptionTex(close)}`;
  }
  if (tag === "mtable") {
    const rows = children.filter((child) => child.tagName.toLowerCase() === "mtr").map((row) => mathMlNodeToTex(row));
    return rows.length ? `\\begin{matrix}${rows.join("\\\\")}\\end{matrix}` : "";
  }
  if (tag === "mtr") return children.map((child) => mathMlNodeToTex(child)).join(" & ");
  if (tag === "mtd") return childTex();
  if (tag === "mspace") return "\\,";
  if (["mover", "munder", "munderover"].includes(tag)) return childTex();
  return childTex();
}

function mathMlToTex(math) {
  const annotation = [...math.querySelectorAll("annotation")].find((node) =>
    /(?:^|[/+.-])tex(?:$|[/+.-])|latex/i.test(node.getAttribute("encoding") || "")
  );
  const annotatedTex = String(annotation && annotation.textContent || "").trim();
  if (annotatedTex) return annotatedTex;
  // Marker commonly stores TeX directly inside <math>, e.g. Fe_{75} or H_c.
  // Treat a text-only math node as TeX so its braces and underscores keep their
  // semantics instead of being escaped as ordinary caption text.
  if (!mathMlElementChildren(math).length) return String(math.textContent || "").trim();
  return mathMlNodeToTex(math).replace(/\s+/g, " ").trim();
}

const CHEMICAL_FORMULA_RE = /\b((?:[A-Z][a-z]?\d+(?:\.\d+)?){2,})\b/g;
const CHEMICAL_COMPONENT_RE = /([A-Z][a-z]?)(\d+(?:\.\d+)?)/g;

function appendChemicalFormula(parent, value) {
  let cursor = 0;
  CHEMICAL_COMPONENT_RE.lastIndex = 0;
  let match;
  while ((match = CHEMICAL_COMPONENT_RE.exec(value))) {
    parent.appendChild(document.createTextNode(value.slice(cursor, match.index)));
    parent.appendChild(document.createTextNode(match[1]));
    const subscript = document.createElement("sub");
    subscript.textContent = match[2];
    parent.appendChild(subscript);
    cursor = match.index + match[0].length;
  }
  parent.appendChild(document.createTextNode(value.slice(cursor)));
}

function restorePlainChemicalFormulas(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (!parent || parent.closest("math, .katex, code, pre, script, style")) {
        return NodeFilter.FILTER_REJECT;
      }
      CHEMICAL_FORMULA_RE.lastIndex = 0;
      return CHEMICAL_FORMULA_RE.test(node.nodeValue || "")
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_REJECT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach((node) => {
    const text = node.nodeValue || "";
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    CHEMICAL_FORMULA_RE.lastIndex = 0;
    let match;
    while ((match = CHEMICAL_FORMULA_RE.exec(text))) {
      fragment.appendChild(document.createTextNode(text.slice(cursor, match.index)));
      appendChemicalFormula(fragment, match[1]);
      cursor = match.index + match[0].length;
    }
    fragment.appendChild(document.createTextNode(text.slice(cursor)));
    node.replaceWith(fragment);
  });
}

function renderFigureCaption(container, source) {
  const raw = String(source || "").trim();
  if (!raw) return;
  const parsed = new DOMParser().parseFromString(raw, "text/html");
  const safe = parsed.body;
  // Older report sessions can contain entity-escaped typesetting such as
  // Fe&lt;sub&gt;75&lt;/sub&gt;. Restore only known presentation tags, leaving all
  // other angle-bracket text untouched.
  const escapedTag = /&lt;(\/?)(sub|sup|b|strong|i|em|br|p|span|math|semantics|mrow|mi|mn|mo|mtext|msub|msup|msubsup|mfrac|msqrt|mroot|mfenced|mtable|mtr|mtd|mspace|mstyle|mover|munder|munderover)(?:\s+display=(?:"(inline|block)"|'(inline|block)'|(inline|block)))?\s*&gt;/gi;
  const walker = document.createTreeWalker(safe, NodeFilter.SHOW_TEXT);
  const escapedNodes = [];
  while (walker.nextNode()) {
    if (escapedTag.test(walker.currentNode.nodeValue || "")) escapedNodes.push(walker.currentNode);
    escapedTag.lastIndex = 0;
  }
  escapedNodes.forEach((node) => {
    const restored = (node.nodeValue || "").replace(escapedTag, (_, closing, tag, a, b, c) => {
      const display = a || b || c || "";
      return `<${closing}${tag}${tag.toLowerCase() === "math" && display ? ` display="${display}"` : ""}>`;
    });
    const template = document.createElement("template");
    template.innerHTML = restored;
    node.replaceWith(template.content);
  });
  safe.querySelectorAll("script, style, iframe, object, embed, link, meta").forEach((node) => node.remove());
  safe.querySelectorAll("math").forEach((math) => {
    const formula = mathMlToTex(math);
    math.replaceWith(document.createTextNode(formula ? `\\(${formula}\\)` : ""));
  });
  safe.querySelectorAll("*").forEach((node) => {
    [...node.attributes].forEach((attribute) => {
      if (attribute.name.toLowerCase().startsWith("on")) node.removeAttribute(attribute.name);
      if (attribute.name.toLowerCase() === "href") node.removeAttribute(attribute.name);
    });
    if (node.tagName.toLowerCase() === "span" && !node.textContent.trim()) node.remove();
  });
  restorePlainChemicalFormulas(safe);
  container.replaceChildren(...safe.childNodes);
  if (window.renderMathInElement) {
    try {
      renderMathInElement(container, {
        delimiters: [
          { left: "\\(", right: "\\)", display: false },
          { left: "$$", right: "$$", display: true },
        ],
        throwOnError: false,
        ignoredTags: ["script", "style", "pre", "code"],
      });
    } catch (error) {
      console.warn("图注公式渲染失败，已保留公式文本：", error);
    }
  }
}

function openFigureLightbox(figure) {
  const existing = document.querySelector(".figure-lightbox");
  if (existing) existing.remove();

  const overlay = document.createElement("div");
  overlay.className = "figure-lightbox";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", `${figure.label} 放大预览`);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "figure-lightbox-close";
  close.textContent = "×";
  close.title = "关闭放大预览";
  const content = document.createElement("div");
  content.className = "figure-lightbox-content";
  const image = document.createElement("img");
  image.className = "figure-lightbox-image";
  image.src = figure.image;
  image.alt = figure.label;
  const label = document.createElement("div");
  label.className = "figure-lightbox-label";
  label.textContent = figure.label;
  content.appendChild(image);
  content.appendChild(label);
  overlay.appendChild(close);
  overlay.appendChild(content);

  const dismiss = () => {
    document.removeEventListener("keydown", onKeyDown);
    overlay.remove();
  };
  const onKeyDown = (event) => {
    if (event.key === "Escape") dismiss();
  };
  close.addEventListener("click", dismiss);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) dismiss();
  });
  document.addEventListener("keydown", onKeyDown);
  document.body.appendChild(overlay);
  close.focus();
}

function createMultiFigurePreviewPanel(figureMap, options = {}) {
  // V2 使用与知识库一致的单图预览：每次只保留最近点击的一个图号。
  // 函数名保持不变，避免影响材料工作区的桥接调用。
  const panel = document.createElement("aside");
  panel.className = "report-figure-panel report-figure-panel-multi";
  const head = document.createElement("div");
  head.className = "report-figure-panel-head";
  head.textContent = "图像预览";
  const body = document.createElement("div");
  body.className = "report-figure-panel-body";
  panel.append(head, body);

  const initialKey = (options.selectedKeys || []).find((key) => figureMap[key]);
  let selectedKey = initialKey || "";
  const onChange = typeof options.onChange === "function" ? options.onChange : null;

  const render = () => {
    body.innerHTML = "";
    const figure = selectedKey && figureMap[selectedKey];
    panel.classList.toggle("is-empty", !figure);
    if (!figure) return;

    const image = document.createElement("img");
    image.className = "report-figure-image";
    image.src = figure.image;
    image.alt = figure.label;
    image.title = "双击放大预览";
    image.addEventListener("dblclick", () => openFigureLightbox(figure));
    const label = document.createElement("div");
    label.className = "report-figure-label";
    label.textContent = figure.label;
    body.append(image);
    if (figure.caption) {
      const caption = document.createElement("div");
      caption.className = "report-figure-caption";
      renderFigureCaption(caption, figure.caption);
      body.append(caption);
    } else {
      body.append(label);
    }
  };

  const toggle = (keys) => {
    const key = (keys || []).find((candidate) => figureMap[candidate]);
    if (!key) return false;
    const shouldRemove = selectedKey === key;
    selectedKey = shouldRemove ? "" : key;
    render();
    if (onChange) onChange(selectedKey ? [selectedKey] : []);
    return !shouldRemove;
  };

  const isSelected = (keys) => Boolean(selectedKey && (keys || []).includes(selectedKey));
  render();
  return { panel, toggle, isSelected, selectedKeys: new Set(selectedKey ? [selectedKey] : []) };
}

function createFigurePreviewPanel(figureMap, options = {}) {
  const panel = document.createElement("aside");
  panel.className = "report-figure-panel";
  const hideWhenEmpty = Boolean(options.hideWhenEmpty);
  const head = document.createElement("div");
  head.className = "report-figure-panel-head";
  head.textContent = "图像预览";
  const body = document.createElement("div");
  body.className = "report-figure-panel-body";
  panel.appendChild(head);
  panel.appendChild(body);

  const alignPanelToReference = (image, reference) => {
    if (!reference || !image || !image.isConnected) return;
    const content = panel.parentElement;
    if (!content) return;
    panel.style.marginTop = "0px";
    const panelRect = panel.getBoundingClientRect();
    const imageRect = image.getBoundingClientRect();
    const referenceRect = reference.getBoundingClientRect();
    const imageCenter = (imageRect.top + imageRect.bottom) / 2;
    const referenceCenter = (referenceRect.top + referenceRect.bottom) / 2;
    const contentRect = content.getBoundingClientRect();
    const imageCenterInPanel = imageCenter - panelRect.top;
    const desiredTop = referenceCenter - imageCenterInPanel;
    const marginTop = Math.max(0, desiredTop - contentRect.top);
    // Put the image center beside the clicked reference. Margin changes the
    // actual layout height, so the report scroll area can still reach it.
    if (Number.isFinite(marginTop)) {
      panel.style.marginTop = `${Math.round(marginTop)}px`;
    }
  };

  const showEmpty = (message) => {
    body.innerHTML = "";
    const placeholder = document.createElement("div");
    placeholder.className = "report-figure-placeholder";
    placeholder.textContent = message;
    body.appendChild(placeholder);
    panel.classList.toggle("is-empty", hideWhenEmpty);
  };

  const show = (keys, reference) => {
    const figures = (keys || []).map((key) => figureMap[key]).filter(Boolean);
    if (!figures.length) {
      showEmpty("该图号没有可用的 JSON 图片。");
      return;
    }
    panel.classList.remove("is-empty");
    body.innerHTML = "";
    const tabs = document.createElement("div");
    tabs.className = "report-figure-tabs";
    const viewer = document.createElement("div");
    viewer.className = "report-figure-viewer";
    const renderFigure = (figure, activeButton) => {
      tabs.querySelectorAll("button").forEach((button) => {
        button.classList.toggle("is-active", button === activeButton);
      });
      viewer.innerHTML = "";
      const image = document.createElement("img");
      image.className = "report-figure-image";
      image.src = figure.image;
      image.alt = figure.label;
      image.title = "双击放大预览";
      image.addEventListener("dblclick", () => openFigureLightbox(figure));
      const label = document.createElement("div");
      label.className = "report-figure-label";
      label.textContent = figure.label;
      const caption = document.createElement("div");
      caption.className = "report-figure-caption";
      renderFigureCaption(caption, figure.caption);
      viewer.appendChild(image);
      if (figure.caption) viewer.appendChild(caption);
      else viewer.appendChild(label);
      const align = () => alignPanelToReference(image, reference);
      image.addEventListener("load", align, { once: true });
      requestAnimationFrame(align);
    };
    figures.forEach((figure, index) => {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.className = "report-figure-tab";
      tab.textContent = figure.label;
      tab.addEventListener("click", () => renderFigure(figure, tab));
      tabs.appendChild(tab);
      if (index === 0) renderFigure(figure, tab);
    });
    if (figures.length > 1) body.appendChild(tabs);
    body.appendChild(viewer);
  };

  showEmpty(Object.keys(figureMap).length ? "点击正文中的图号查看图片。" : "该报告没有可用的 JSON 图片。");
  return { panel, show };
}

function prepareReportFigureReferences(container, figureMap, showPreview, isPreviewSelected, registerReference) {
  // 原始 Markdown 仍保留图片用于导出；网页报告不直接铺开图片。
  container.querySelectorAll("img").forEach((image) => {
    const parent = image.parentElement;
    image.remove();
    if (parent && !parent.textContent.trim() && !parent.querySelector("img")) parent.remove();
  });
  if (!Object.keys(figureMap).length) return;

  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (!parent || parent.closest("button, code, pre, .katex")) return NodeFilter.FILTER_REJECT;
      REPORT_FIGURE_REF_RE.lastIndex = 0;
      return REPORT_FIGURE_REF_RE.test(node.nodeValue || "") ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach((node) => {
    const text = node.nodeValue || "";
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    REPORT_FIGURE_REF_RE.lastIndex = 0;
    let match;
    while ((match = REPORT_FIGURE_REF_RE.exec(text))) {
      const keys = figureKeysFromReportReference(match[1], figureMap);
      if (!keys.length) continue;
      fragment.appendChild(document.createTextNode(text.slice(cursor, match.index)));
      const trigger = document.createElement("button");
      trigger.type = "button";
      trigger.className = "report-figure-ref";
      trigger.dataset.figureKeys = JSON.stringify(keys);
      trigger.textContent = match[0];
      trigger.title = "在右侧查看已验证图片";
      if (typeof isPreviewSelected === "function") {
        const isSelected = isPreviewSelected(keys);
        trigger.classList.toggle("is-active", isSelected);
        trigger.setAttribute("aria-pressed", String(isSelected));
      }
      if (typeof registerReference === "function") registerReference(keys, trigger);
      trigger.addEventListener("click", () => {
        const isSelected = showPreview(keys, trigger);
        if (typeof isSelected === "boolean") {
          // 单图模式下切换到新 Fig 时，同时清除旧 Fig 的高亮状态。
          container.querySelectorAll(".report-figure-ref[data-figure-keys]").forEach((button) => {
            let buttonKeys = [];
            try { buttonKeys = JSON.parse(button.dataset.figureKeys || "[]"); } catch (_) {}
            const active = typeof isPreviewSelected === "function" && isPreviewSelected(buttonKeys);
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-pressed", String(active));
          });
        }
      });
      fragment.appendChild(trigger);
      cursor = match.index + match[0].length;
    }
    if (cursor) {
      fragment.appendChild(document.createTextNode(text.slice(cursor)));
      node.replaceWith(fragment);
    }
  });
}

function renderReportMarkdown(container, markdown, figureMap, showPreview, isPreviewSelected, registerReference) {
  renderMarkdown(container, markdown);
  prepareReportFigureReferences(container, figureMap, showPreview, isPreviewSelected, registerReference);
}

// 把结果按来源 PDF 聚合成模块；模块内按格式聚合
function groupResults(results) {
  const map = new Map();
  results.forEach((r) => {
    if (!map.has(r.source)) map.set(r.source, { source: r.source, formats: {} });
    map.get(r.source).formats[r.format] = r;
  });
  return Array.from(map.values());
}

/* ---------- 结果块：每次运行生成一个"转换结果预览（N）"，各自独立、全部保留 ---------- */
function createRunBlock(runNo, results) {
  const state = {
    modules: groupResults(results),
    activeModule: 0,
    activeFormat: null,
    allResults: results,
  };

  // —— 构建 DOM 骨架 ——
  const box = document.createElement("div");
  box.className = "results";

  const header = document.createElement("div");
  header.className = "results-header";
  const title = document.createElement("span");
  title.className = "results-title";
  title.textContent = `转换结果预览（${runNo}）`;
  const collapseBtn = document.createElement("button");
  collapseBtn.type = "button";
  collapseBtn.className = "collapse-btn";
  collapseBtn.textContent = "收回 ▲";
  header.appendChild(title);
  header.appendChild(collapseBtn);

  const inner = document.createElement("div");
  inner.className = "results-inner";

  const moduleTabsEl = document.createElement("div");
  moduleTabsEl.className = "module-tabs";

  const moduleBody = document.createElement("div");
  moduleBody.className = "module-body";

  const formatTabsEl = document.createElement("div");
  formatTabsEl.className = "format-tabs is-hidden";

  const actions = document.createElement("div");
  actions.className = "module-actions";
  const dlWrap = document.createElement("div");
  dlWrap.className = "dl-wrap";
  const dlBtn = document.createElement("button");
  dlBtn.type = "button";
  dlBtn.className = "rc-dl";
  dlBtn.textContent = "⬇ 下载";
  const dlMenu = document.createElement("div");
  dlMenu.className = "dl-menu is-hidden";
  dlWrap.appendChild(dlBtn);
  dlWrap.appendChild(dlMenu);
  actions.appendChild(dlWrap);
  const joinBtn = document.createElement("button");
  joinBtn.type = "button";
  joinBtn.className = "rc-join";
  joinBtn.textContent = "加入提取工作区";
  const joinWrap = document.createElement("div");
  joinWrap.className = "join-wrap";
  const joinMenu = document.createElement("div");
  joinMenu.className = "dl-menu join-menu is-hidden";
  joinWrap.appendChild(joinBtn);
  joinWrap.appendChild(joinMenu);
  actions.appendChild(joinWrap);

  const bodyEl = document.createElement("div");
  bodyEl.className = "rc-body";

  moduleBody.appendChild(formatTabsEl);
  moduleBody.appendChild(actions);
  moduleBody.appendChild(bodyEl);
  inner.appendChild(moduleTabsEl);
  inner.appendChild(moduleBody);
  box.appendChild(header);
  box.appendChild(inner);

  // —— 渲染逻辑（作用域内闭包，互不影响） ——
  function renderPreview() {
    bodyEl.innerHTML = "";
    const m = state.modules[state.activeModule];
    if (!m) return;
    const item = m.formats[state.activeFormat];
    if (!item) return;

    if (item.format === "html") {
      // 用 sandbox 的 iframe 渲染 html（禁脚本，避免 XSS）
      const iframe = document.createElement("iframe");
      iframe.className = "preview-iframe";
      iframe.setAttribute("sandbox", "");
      iframe.srcdoc = item.content;
      bodyEl.appendChild(iframe);
      return;
    }
    if (item.format === "markdown" && window.marked) {
      const div = document.createElement("div");
      div.className = "preview-md";
      renderMarkdown(div, item.content);
      bodyEl.appendChild(div);
      return;
    }
    const pre = document.createElement("pre");
    pre.className = "preview-text";
    pre.textContent = item.content;
    bodyEl.appendChild(pre);
  }

  function renderModuleTabs() {
    moduleTabsEl.innerHTML = "";
    state.modules.forEach((m, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "mod-tab" + (i === state.activeModule ? " active" : "");
      b.textContent = m.source;
      b.title = m.source;
      b.addEventListener("click", () => {
        state.activeModule = i;
        state.activeFormat = null;       // 切模块时重置格式选择
        renderAll();
      });
      moduleTabsEl.appendChild(b);
    });
  }

  function renderFormatTabs() {
    formatTabsEl.innerHTML = "";
    const m = state.modules[state.activeModule];
    if (!m) return;
    const fmts = Object.keys(m.formats);
    if (state.activeFormat == null || !m.formats[state.activeFormat]) {
      state.activeFormat = fmts[0];
    }
    fmts.forEach((f) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "fmt-tab" + (f === state.activeFormat ? " active" : "");
      b.textContent = f;
      b.addEventListener("click", () => {
        state.activeFormat = f;
        renderFormatTabs();
        renderPreview();
      });
      formatTabsEl.appendChild(b);
    });
    formatTabsEl.classList.toggle("is-hidden", fmts.length <= 1);
  }

  function renderDownloadMenu() {
    dlMenu.innerHTML = "";
    if (!state.allResults.length) return;

    const bySource = new Map();
    state.allResults.forEach((r, i) => {
      if (!bySource.has(r.source)) bySource.set(r.source, []);
      bySource.get(r.source).push({ r, i });
    });

    const list = document.createElement("div");
    list.className = "dl-list";
    bySource.forEach((items, source) => {
      const head = document.createElement("div");
      head.className = "dl-file-head";
      head.textContent = source;
      list.appendChild(head);

      items.forEach(({ r, i }) => {
        const item = document.createElement("label");
        item.className = "dl-item";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.className = "dl-check";
        cb.value = String(i);
        const info = document.createElement("span");
        info.className = "dl-info";
        info.innerHTML =
          `<span class="dl-fmt">${r.format}</span>` +
          `<span class="dl-name">${r.filename}</span>`;
        item.appendChild(cb);
        item.appendChild(info);
        list.appendChild(item);
      });
    });
    dlMenu.appendChild(list);

    const footer = document.createElement("div");
    footer.className = "dl-menu-footer";
    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = "dl-confirm";
    confirm.textContent = "下载";
    confirm.addEventListener("click", (e) => {
      e.stopPropagation();
      const checked = Array.from(dlMenu.querySelectorAll(".dl-check:checked"));
      if (!checked.length) {
        confirm.textContent = "请先勾选文件";
        setTimeout(() => (confirm.textContent = "下载"), 1200);
        return;
      }
      checked.forEach((cb, idx) => {
        const r = state.allResults[Number(cb.value)];
        setTimeout(() => download(r.filename, r.content, r.mime), idx * 300);
      });
      dlMenu.classList.add("is-hidden");
    });
    footer.appendChild(confirm);
    dlMenu.appendChild(footer);
  }

  function renderJoinMenu() {
    joinMenu.innerHTML = "";
    const modules = state.modules.filter(
      (module) => sourcePdfFiles.has(module.source) && module.formats.markdown
    );
    if (!modules.length) {
      const empty = document.createElement("div");
      empty.className = "dl-file-head";
      empty.textContent = "暂无可加入的 Markdown 转换结果";
      joinMenu.appendChild(empty);
      return;
    }

    const list = document.createElement("div");
    list.className = "dl-list";
    modules.forEach((module, index) => {
      const item = document.createElement("label");
      item.className = "dl-item";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "dl-check join-check";
      cb.value = String(index);
      const info = document.createElement("span");
      info.className = "dl-info";
      info.innerHTML =
        `<span class="dl-fmt">Markdown</span>` +
        `<span class="dl-name">${module.source}</span>`;
      item.appendChild(cb);
      item.appendChild(info);
      list.appendChild(item);
    });
    joinMenu.appendChild(list);

    const footer = document.createElement("div");
    footer.className = "dl-menu-footer";
    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = "dl-confirm";
    confirm.textContent = "加入";
    confirm.addEventListener("click", async (e) => {
      e.stopPropagation();
      const checked = Array.from(joinMenu.querySelectorAll(".join-check:checked"));
      if (!checked.length) {
        confirm.textContent = "请先选择文件";
        setTimeout(() => { confirm.textContent = "加入"; }, 1200);
        return;
      }
      confirm.disabled = true;
      confirm.textContent = "处理中…";
      try {
        for (const cb of checked) {
          const module = modules[Number(cb.value)];
          const pdf = sourcePdfFiles.get(module.source);
          const markdown = module.formats.markdown ? module.formats.markdown.content : "";
          await preparePaperForMaterial(module.source, markdown, pdf, { origin: "conversion" });
        }
        joinMenu.classList.add("is-hidden");
        joinBtn.textContent = "已加入";
        setTimeout(() => { joinBtn.textContent = "加入提取工作区"; }, 1800);
      } catch (err) {
        alert("加入提取工作区失败：" + err.message);
        confirm.disabled = false;
        confirm.textContent = "加入";
      }
    });
    footer.appendChild(confirm);
    joinMenu.appendChild(footer);
  }

  function renderAll() {
    renderModuleTabs();
    renderFormatTabs();
    renderPreview();
    renderDownloadMenu();
    renderJoinMenu();
  }

  // —— 事件 ——
  dlBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    joinMenu.classList.add("is-hidden");
    dlMenu.classList.toggle("is-hidden");
  });
  joinBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    dlMenu.classList.add("is-hidden");
    joinMenu.classList.toggle("is-hidden");
  });
  document.addEventListener("click", (e) => {
    if (!dlMenu.classList.contains("is-hidden") &&
        !dlMenu.contains(e.target) && e.target !== dlBtn) {
      dlMenu.classList.add("is-hidden");
    }
    if (!joinMenu.classList.contains("is-hidden") &&
        !joinMenu.contains(e.target) && e.target !== joinBtn) {
      joinMenu.classList.add("is-hidden");
    }
  });
  collapseBtn.addEventListener("click", () => {
    const collapsed = inner.classList.toggle("is-hidden");
    collapseBtn.textContent = collapsed ? "展开 ▼" : "收回 ▲";
  });

  renderAll();
  return box;
}

// ---------- Marker 2 整批转换：后端固定最多三篇并发，完成后只下载 ZIP ----------
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function submitConversionBatch(selectedFiles, rows, runToken) {
  const fd = new FormData();
  selectedFiles.forEach((file) => fd.append("files", file, file.name));
  const resp = await fetch("/api/conversion/batch", { method: "POST", body: fd });
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try { msg += `：${(await resp.json()).error || ""}`; } catch (_) {}
    throw new Error(msg);
  }
  const { task_id } = await resp.json();
  if (runToken !== convertRunToken || stopConvertFlag) {
    await fetch("/api/conversion/stop/" + task_id, { method: "POST" }).catch(() => {});
    throw new Error("已停止");
  }
  currentConvertTaskId = task_id;
  while (true) {
    await sleep(600);
    if (runToken !== convertRunToken || stopConvertFlag) throw new Error("已停止");
    const pr = await fetch(`/api/conversion/progress/${task_id}`);
    if (!pr.ok) throw new Error(`进度查询失败（HTTP ${pr.status}）`);
    const progress = await pr.json();
    progress.files.forEach((item, index) => {
      const ui = rows[index];
      if (!ui) return;
      const pct = Math.min(100, Math.round(item.percent || 0));
      ui.fill.style.width = pct + "%";
      ui.pct.textContent = item.status === "queued" ? "等待" :
        item.status === "failed" ? "失败" :
        item.status === "cancelled" ? "已停止" : `${pct}%`;
      ui.stage.textContent = item.error || item.stage || "";
      ui.fill.classList.toggle("done", item.status === "completed");
      ui.fill.classList.toggle("fail", item.status === "failed");
    });
    if (progress.cancelled) throw new Error("已停止");
    if (progress.done) {
      if (!progress.download_url) throw new Error(progress.error || "没有可下载的转换结果");
      return progress;
    }
  }
}

function appendConversionDownload(result, selectedFiles) {
  const card = document.createElement("div");
  card.className = "run-block";
  const title = document.createElement("div");
  title.className = "run-title";
  title.textContent = selectedFiles.length === 1
    ? `${selectedFiles[0].name} 转换完成`
    : `${selectedFiles.length} 篇 PDF 转换完成`;
  const copy = document.createElement("div");
  copy.className = "tip";
  copy.textContent = "Markdown 与独立图片已按论文文件夹整理，预览已取消以避免重复解析与额外上下文。";
  const link = document.createElement("a");
  link.className = "choose";
  link.href = result.download_url;
  link.textContent = selectedFiles.length === 1 ? "下载论文 ZIP" : "下载批量 ZIP";
  link.setAttribute("download", "");
  card.append(title, copy, link);
  runsEl.prepend(card);
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (isConverting) { stopConversion(); return; }   // 转换中点按钮 = 停止
  if (!files.length) {
    status.textContent = "请先选择至少一个 PDF 文件";
    return;
  }

  progressEl.innerHTML = "";
  progressEl.classList.remove("is-hidden");
  const rows = files.map((f) => {
    const row = document.createElement("div");
    row.className = "prog-row";

    const name = document.createElement("div");
    name.className = "prog-name";
    name.textContent = f.name;
    name.title = f.name;

    const stage = document.createElement("div");
    stage.className = "prog-stage";
    stage.textContent = "";

    const track = document.createElement("div");
    track.className = "prog-track";
    const fill = document.createElement("div");
    fill.className = "prog-fill";
    track.appendChild(fill);

    const pct = document.createElement("div");
    pct.className = "prog-pct";
    pct.textContent = "等待";

    row.appendChild(name);
    row.appendChild(stage);
    row.appendChild(track);
    row.appendChild(pct);
    progressEl.appendChild(row);
    return { fill, pct, stage };
  });

  stopConvertFlag = false;
  const runToken = ++convertRunToken;
  isConverting = true;
  btn.classList.add("is-busy");   // 配合 :hover 变红，显示「停止转换」
  status.textContent = `已提交 ${files.length} 篇 PDF；最多同时转换 3 篇…`;

  try {
    const result = await submitConversionBatch([...files], rows, runToken);
    if (runToken !== convertRunToken) return;
    appendConversionDownload(result, files);
    const completed = result.files.filter((item) => item.status === "completed").length;
    status.textContent = `完成：${completed}/${files.length} 篇已打包，可直接下载 ZIP。`;
  } catch (error) {
    if (runToken === convertRunToken && !stopConvertFlag) {
      status.textContent = `转换失败：${error.message}`;
    }
  } finally {
    if (runToken === convertRunToken) resetConvertBtn();
  }
});

// 转换按钮 hover：进行中时文案切换为「停止转换」（红色由 CSS .is-busy:hover 控制）
btn.addEventListener("mouseenter", () => {
  if (isConverting) btn.textContent = "停止转换";
});
btn.addEventListener("mouseleave", () => {
  if (isConverting) btn.textContent = "转换";
});

// 复位转换按钮到初始状态
function resetConvertBtn() {
  isConverting = false;
  stopConvertFlag = false;
  currentConvertTaskId = null;
  btn.classList.remove("is-busy");
  btn.textContent = "转换";
}

// 停止转换：通知后端取消，前端尽快退出轮询；轮询检测到标志后由收尾逻辑复位
function stopConversion() {
  stopConvertFlag = true;
  convertRunToken += 1;
  const taskId = currentConvertTaskId;
  if (taskId) {
    fetch("/api/conversion/stop/" + taskId, { method: "POST" }).catch(() => {});
  }
  // 立即释放按钮；旧 async 流程由 runToken 失效，不再有权重置新任务状态。
  isConverting = false;
  currentConvertTaskId = null;
  btn.classList.remove("is-busy");
  btn.textContent = "转换";
  status.textContent = "已停止转换。";
}

/* ==================== 共享：PDF 转换后产出的 MD 文件 ==================== */
// 论文信息提取 / 针对材料信息提取 两个工作区都从这里取数据；刷新即清空（符合"刷新全重置"）
const convertedFiles = [];   // { id, name, kind: 'md', content, figureIndex, ready }
function addConvertedFile(name, content, options = {}) {
  let rec = convertedFiles.find((c) => c.name === name);
  if (!rec) {
    rec = {
      id: "cf_" + Date.now() + "_" + Math.random().toString(36).slice(2, 7),
      name,
      kind: "md",
      content,
      figureIndex: options.figureIndex || [],
      serverDocumentId: options.serverDocumentId || "",
      ready: options.ready !== false,
      sourcePdf: options.sourcePdf || null,
      sourceType: options.sourceType || "MD",
      prepareStatus: options.ready === false ? "queued" : "completed",
      preparePercent: options.ready === false ? 0 : 100,
      prepareError: "",
      prepareElapsed: 0,
      prepareStartedAt: 0,
    };
    convertedFiles.push(rec);
  } else {
    rec.content = content;
    if (options.figureIndex) rec.figureIndex = options.figureIndex;
    if (options.serverDocumentId) rec.serverDocumentId = options.serverDocumentId;
    if (options.ready != null) rec.ready = options.ready;
    if (options.sourcePdf) rec.sourcePdf = options.sourcePdf;
    if (options.sourceType) rec.sourceType = options.sourceType;
  }
  return rec;
}

// 将转换结果关联的原始 PDF 预处理为材料分析记录。已有 Markdown 会被保留，
// 后端只补内部 JSON 与 FigureIndex；没有 Markdown 时则同时生成分析用 Markdown。
async function preparePaperForMaterial(name, markdown, pdf, options = {}) {
  if (!(pdf instanceof File)) {
    throw new Error("找不到本次转换使用的原始 PDF，请重新选择并转换该文件");
  }

  const origin = options.origin || "conversion";
  let rec = convertedFiles.find((p) => p.name === name);
  // 同名论文以格式转换区为准：本地添加不覆盖或重复处理它。
  if (rec && origin === "local" && !options.retry) return null;

  if (!rec) {
    rec = addConvertedFile(name, markdown || "", {
      ready: false,
      sourcePdf: pdf,
      sourceType: "PDF",
    });
  } else {
    // 格式转换区晚到时接管同名的本地任务，防止旧任务继续回写该条目。
    if (rec.prepareOrigin === "local" && rec.prepareTaskId) {
      fetch("/api/stop_paper_prepare/" + rec.prepareTaskId, { method: "POST" }).catch(() => {});
    }
    rec.content = markdown || "";
    rec.figureIndex = [];
    rec.ready = false;
    rec.sourcePdf = pdf;
    rec.sourceType = "PDF";
  }
  rec.prepareStatus = "queued";
  rec.preparePercent = 0;
  rec.prepareError = "";
  rec.prepareElapsed = 0;
  rec.prepareStartedAt = Date.now();
  const prepareRunId = (rec.prepareRunId || 0) + 1;
  rec.prepareRunId = prepareRunId;
  rec.prepareOrigin = origin;
  rec.prepareTaskId = "";
  if (options.select) {
    matSelected.add(rec.id);
    matColInfo.classList.remove("is-hidden");
    updateSplitters();
  }
  rec.prepareStage = "正在提交 PDF";
  rec.prepareStatus = "queued";
  renderMatFiles();
  renderAttachPanel();

  const markPrepareFailure = (message, status = "failed") => {
    if (rec.prepareRunId !== prepareRunId) return;
    rec.ready = false;
    rec.prepareStatus = status;
    rec.prepareError = message || (status === "cancelled" ? "PDF 预处理已取消" : "PDF 预处理失败");
    rec.prepareStage = rec.prepareError;
    rec.prepareTaskId = "";
    rec.prepareElapsed = Math.max(0, Math.round((Date.now() - rec.prepareStartedAt) / 1000));
    renderMatFiles();
    renderAttachPanel();
  };

  const fd = new FormData();
  fd.append("file", pdf, pdf.name);
  // Markdown 常含 base64 图片，作为文件 part 上传以避开普通表单字段的大小限制。
  const markdownBlob = new Blob([markdown || ""], { type: "text/markdown;charset=utf-8" });
  fd.append("markdown_file", markdownBlob, "converted.md");
  let response;
  let started;
  try {
    response = await fetch("/api/prepare_paper", { method: "POST", body: fd });
    started = await response.json().catch(() => ({}));
  } catch (error) {
    const message = error && error.message ? error.message : "PDF 预处理任务提交失败";
    markPrepareFailure(message);
    throw error instanceof Error ? error : new Error(message);
  }
  if (rec.prepareRunId !== prepareRunId) {
    if (started.task_id) fetch("/api/stop_paper_prepare/" + started.task_id, { method: "POST" }).catch(() => {});
    return rec;
  }
  if (!response.ok || !started.task_id) {
    const message = started.error || "PDF 预处理任务创建失败";
    markPrepareFailure(message);
    throw new Error(message);
  }
  rec.prepareTaskId = started.task_id;

  while (true) {
    await sleep(700);
    if (rec.prepareRunId !== prepareRunId) return rec;
    const progressResponse = await fetch("/api/paper_prepare_progress/" + started.task_id);
    const progress = await progressResponse.json().catch(() => ({}));
    if (rec.prepareRunId !== prepareRunId) return rec;
    if (!progressResponse.ok) {
      const message = progress.error || "PDF 预处理进度查询失败";
      markPrepareFailure(message);
      throw new Error(message);
    }
    rec.prepareStage = progress.stage || "正在处理";
    rec.preparePercent = Number(progress.percent || 0);
    rec.prepareStatus = progress.cancelled ? "cancelled" : (progress.done ? (progress.success ? "completed" : "failed") : "processing");
    renderMatFiles();
    if (!progress.done) continue;
    if (!progress.success || progress.cancelled || !progress.result) {
      const message = progress.error || (progress.cancelled ? "PDF 预处理已取消" : "PDF 预处理失败");
      markPrepareFailure(message, progress.cancelled ? "cancelled" : "failed");
      throw new Error(message);
    }

    rec.content = "";
    rec.figureIndex = progress.result.figure_index || [];
    rec.serverDocumentId = progress.result.document_id || started.document_id || "";
    rec.ready = true;
    rec.prepareStatus = "completed";
    rec.preparePercent = 100;
    rec.prepareError = "";
    rec.prepareElapsed = Math.max(0, Math.round((Date.now() - rec.prepareStartedAt) / 1000));
    rec.prepareStage = "";
    rec.prepareTaskId = "";
    rec.sourcePdf = pdf;
    rec.sourceType = "PDF";
    renderMatFiles();
    renderAttachPanel();
    return rec;
  }
}

/* ==================== 工作区 2：论文信息提取（聊天 + 已转换文件/＋） ==================== */
const chatHistoryEl = document.getElementById("chat-history");
const chatHistoryEmpty = document.getElementById("chat-history-empty");
const newChatBtn = document.getElementById("new-chat-btn");
const chatMessagesEl = document.getElementById("chat-messages");
const chatWelcome = document.getElementById("chat-welcome");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const attachChipsEl = document.getElementById("attach-chips");

// 工具按钮：已转换文件 / ＋
const convFilesBtn = document.getElementById("conv-files-btn");
const plusBtn = document.getElementById("plus-btn");
const paperFileInputChat = document.getElementById("paper-file-input-chat");
const attachPanel = document.getElementById("attach-panel");
const attachPanelList = document.getElementById("attach-panel-list");

let chatAttachPapers = new Set();   // 本次对话已附加的论文 id（来自知识库）
let chatAttachFiles = [];           // 本次对话通过 ＋ 上传的本地文件（File 对象）
const conversations = [];           // 内存中的对话历史（刷新即清空）
let activeConv = null;
let busy = false;

// —— 「已转换文件」弹出面板 ——
function renderAttachPanel() {
  attachPanelList.innerHTML = "";
  if (!convertedFiles.length) {
    attachPanelList.innerHTML = '<div class="attach-none">无</div>';
    return;
  }
  convertedFiles.forEach((p) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "attach-item" + (chatAttachPapers.has(p.id) ? " selected" : "");
    const tag = document.createElement("span");
    tag.className = "attach-tag";
    tag.textContent = "MD";
    const name = document.createElement("span");
    name.className = "attach-item-name";
    name.textContent = p.name;
    name.title = p.name;
    const chk = document.createElement("span");
    chk.className = "attach-check";
    chk.textContent = "✓";
    item.appendChild(tag);
    item.appendChild(name);
    item.appendChild(chk);
    item.addEventListener("click", () => {
      if (chatAttachPapers.has(p.id)) chatAttachPapers.delete(p.id);
      else chatAttachPapers.add(p.id);
      renderAttachPanel();
      renderChips();
    });
    attachPanelList.appendChild(item);
  });
}

convFilesBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  attachPanel.classList.toggle("is-hidden");
});
document.addEventListener("click", (e) => {
  if (!attachPanel.classList.contains("is-hidden") &&
      !attachPanel.contains(e.target) && e.target !== convFilesBtn) {
    attachPanel.classList.add("is-hidden");
  }
});

// —— ＋ 上传本地 md/pdf：直接作为本次对话附件（第一版行为，不落任何库）——
plusBtn.addEventListener("click", () => paperFileInputChat.click());
paperFileInputChat.addEventListener("change", () => {
  for (const f of paperFileInputChat.files) chatAttachFiles.push(f);
  paperFileInputChat.value = "";
  renderChips();
});

// —— 附件 chips（论文 + 本地文件）——
function renderChips() {
  attachChipsEl.innerHTML = "";
  convertedFiles.forEach((p) => {
    if (!chatAttachPapers.has(p.id)) return;
    const chip = document.createElement("span");
    chip.className = "attach-chip";
    chip.innerHTML = `📄 <span class="chip-name"></span>`;
    chip.querySelector(".chip-name").textContent = p.name;
    const x = document.createElement("button");
    x.type = "button";
    x.className = "chip-x";
    x.textContent = "×";
    x.addEventListener("click", () => {
      chatAttachPapers.delete(p.id);
      renderAttachPanel();
      renderChips();
    });
    chip.appendChild(x);
    attachChipsEl.appendChild(chip);
  });
  chatAttachFiles.forEach((f, i) => {
    const chip = document.createElement("span");
    chip.className = "attach-chip local";
    chip.innerHTML = `📎 <span class="chip-name"></span>`;
    chip.querySelector(".chip-name").textContent = f.name;
    const x = document.createElement("button");
    x.type = "button";
    x.className = "chip-x";
    x.textContent = "×";
    x.addEventListener("click", () => {
      chatAttachFiles.splice(i, 1);
      renderChips();
    });
    chip.appendChild(x);
    attachChipsEl.appendChild(chip);
  });
}

// —— 历史对话侧栏 ——
function renderHistory() {
  chatHistoryEl.querySelectorAll(".chat-history-item").forEach((n) => n.remove());
  chatHistoryEmpty.classList.toggle("is-hidden", conversations.length > 0);
  conversations.forEach((c) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "chat-history-item" + (c === activeConv ? " active" : "");
    item.textContent = c.title;
    item.title = c.title;
    item.addEventListener("click", () => {
      activeConv = c;
      renderHistory();
      renderMessages();
    });
    chatHistoryEl.appendChild(item);
  });
}

function renderMessages() {
  chatMessagesEl.innerHTML = "";
  if (!activeConv || !activeConv.messages.length) {
    chatMessagesEl.appendChild(chatWelcome);
    chatWelcome.classList.remove("is-hidden");
    return;
  }
  chatWelcome.classList.add("is-hidden");
  activeConv.messages.forEach((m) => {
    const row = document.createElement("div");
    row.className = "msg-row " + (m.role === "user" ? "msg-user" : "msg-ai");
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    if (m.atts && m.atts.length) {
      const atts = document.createElement("div");
      atts.className = "msg-atts";
      m.atts.forEach((a) => {
        const s = document.createElement("span");
        s.className = "msg-att";
        s.textContent = a;
        atts.appendChild(s);
      });
      bubble.appendChild(atts);
    }
    const body = document.createElement("div");
    body.className = "msg-text";
    renderMarkdown(body, m.text);
    bubble.appendChild(body);
    row.appendChild(bubble);
    chatMessagesEl.appendChild(row);
  });
  chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
}

// —— 发送 / 接收（第一版：占位回复，不调用真实抽取）——
function sendMessage() {
  const text = chatInput.value.trim();
  if ((!text && chatAttachPapers.size === 0 && chatAttachFiles.length === 0) || busy) return;

  if (!activeConv) {
    activeConv = {
      id: Date.now(),
      title: text ? (text.length > 18 ? text.slice(0, 18) + "…" : text) : "（论文对话）",
      messages: [],
    };
    conversations.unshift(activeConv);
  }
  const attNames = [
    ...[...chatAttachPapers].map((id) => (convertedFiles.find((p) => p.id === id) || {}).name).filter(Boolean),
    ...chatAttachFiles.map((f) => f.name),
  ];
  activeConv.messages.push({ role: "user", text, atts: attNames });
  activeConv.messages.push({ role: "ai", text: "功能开发中，敬请期待。" });
  chatInput.value = "";
  chatInput.style.height = "auto";
  renderHistory();
  renderMessages();
  // 发送后清空附件
  chatAttachPapers.clear();
  chatAttachFiles = [];
  renderChips();
  renderAttachPanel();
}

sendBtn.addEventListener("click", sendMessage);
chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
chatInput.addEventListener("input", () => {
  chatInput.style.height = "auto";
  chatInput.style.height = Math.min(chatInput.scrollHeight, 160) + "px";
});
newChatBtn.addEventListener("click", () => {
  activeConv = null;
  renderHistory();
  renderMessages();
});

/* ==================== 工作区 3：针对材料信息提取（三栏） ==================== */
const matFileListEl = document.getElementById("mat-file-list");
const matExtractBtn = document.getElementById("mat-extract-btn");
const matInfoBody = document.getElementById("mat-info-body");
const matExportBtn = document.getElementById("mat-export-btn");
const matExportDialog = document.getElementById("mat-export-dialog");
const matExportReportList = document.getElementById("mat-export-report-list");
const matExportConfirmBtn = document.getElementById("mat-export-confirm");
const matExportCancelBtn = document.getElementById("mat-export-cancel");
const matExportCancelBottomBtn = document.getElementById("mat-export-cancel-bottom");
const matColOrig = document.getElementById("mat-col-orig");
const matColInfo = document.getElementById("mat-col-info");
const matPlusBtn = document.getElementById("mat-plus-btn");
const matFileInput = document.getElementById("mat-file-input");
const matModelSelect = document.getElementById("mat-model-select");
const matColFiles = document.getElementById("mat-col-files");
const matSplit1 = document.getElementById("mat-split-1");
const matSplit2 = document.getElementById("mat-split-2");
let matSelected = new Set();
let matResults = new Map(); // paperId -> { name, part1, part2, part3 }：已提取结果，增量累积，刷新页面才清
let matExpanded = new Set(); // 当前展开查看的论文 id，跨「增量提取」渲染保留查看状态
let matExtracting = false; // 防止并发提取
let stopMaterialFlag = false;     // 用户已点停止，前端尽快退出轮询
let currentMaterialTaskId = null; // 当前提取任务 id（用于停止）
let materialRunToken = 0;
let materialCancelRequested = false;
let matModels = [];               // 后端可用模型列表 [{name,label,default,configured,order}]
let matModelLabels = {};          // name -> label，渲染报告时把 provider 键映射成展示名

// 从后端拉取可用模型列表，填充「材料信息提取」的模型选择下拉框（直接显示模型名）
async function loadMatModels() {
  try {
    const r = await fetch("/api/models");
    const data = await r.json().catch(() => ({}));
    matModels = (data && data.models) || [];
    matModelLabels = {};
    const sel = matModelSelect;
    sel.innerHTML = "";
    matModels.forEach((m) => {
      matModelLabels[m.name] = m.label || m.name;
      const o = document.createElement("option");
      o.value = m.name;
      o.textContent = m.label || m.name;
      if (m.default) o.selected = true;       // 默认选中 default_provider
      if (!m.configured) o.disabled = true;    // 未配置 key 的模型置灰不可选
      sel.appendChild(o);
    });
  } catch (e) {
    // 接口异常时兜底显示一个默认项，避免下拉框空白
    matModelSelect.innerHTML = '<option value="mimo-v2.5-pro">Xiaomi MiMo-V2.5-Pro</option>';
  }
}

// 分栏线可见性：仅当相邻栏显示时才出现
function updateSplitters() {
  const infoVisible = !matColInfo.classList.contains("is-hidden");
  const origVisible = !matColOrig.classList.contains("is-hidden");
  matSplit1.style.display = infoVisible ? "block" : "none";
  matSplit2.style.display = origVisible ? "block" : "none";
}

// 拖拽分栏线调节相邻栏宽窄
function enableSplit(splitter, col, mode) {
  splitter.addEventListener("mousedown", (e) => {
    e.preventDefault();
    splitter.classList.add("dragging");
    const startX = e.clientX;
    const startW = col.getBoundingClientRect().width;
    const minW = mode === "right" ? 180 : 260;
    const maxW = mode === "right" ? 520 : 720;
    const onMove = (ev) => {
      const delta = ev.clientX - startX;
      let w = mode === "right" ? startW + delta : startW - delta;
      w = Math.max(minW, Math.min(maxW, w));
      col.style.flex = "0 0 " + Math.round(w) + "px";
    };
    const onUp = () => {
      splitter.classList.remove("dragging");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.userSelect = "";
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    document.body.style.userSelect = "none";
  });
}
enableSplit(matSplit1, matColFiles, "right");  // 拖第一根线 → 调「论文列表」宽度
enableSplit(matSplit2, matColOrig, "left");    // 拖第二根线 → 调「原文预览」宽度
updateSplitters();

function renderMatFiles() {
  matFileListEl.innerHTML = "";
  // 清理 matSelected 中已不存在于 convertedFiles 的失效 id
  for (const id of matSelected) {
    if (!convertedFiles.some((p) => p.id === id)) matSelected.delete(id);
  }
  // 材料工作区第一栏显示会话内共享论文记录（PDF 预处理完成或本地 Markdown）。
  const mdFiles = convertedFiles.filter((p) => p.kind === "md");
  if (!mdFiles.length) {
    matFileListEl.innerHTML =
      '<div class="mat-empty">暂无转换后的 MD 文件，请先在 PDF 转换工作区转换（生成 markdown），或用下方「＋」添加本地 PDF。</div>';
    return;
  }
  mdFiles.forEach((p) => {
    const item = document.createElement("label");
    item.className = "mat-file-item" + (matSelected.has(p.id) ? " selected" : "");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "mat-cb";
    cb.checked = matSelected.has(p.id);
    cb.addEventListener("change", () => {
      if (cb.checked) matSelected.add(p.id);
      else matSelected.delete(p.id);
      renderMatFiles();
      if (matSelected.size > 0) {
        matColInfo.classList.remove("is-hidden");
        updateSplitters();
      }
    });
    const tag = document.createElement("span");
    tag.className = "attach-tag";
    tag.textContent = p.ready ? (p.sourceType || "MD") : "准备中";
    const name = document.createElement("span");
    name.className = "mat-file-name";
    name.textContent = p.name;
    name.title = p.name;
    if (!p.ready && p.prepareStage) {
      name.textContent += "（" + p.prepareStage + "）";
    }
    // 删除按钮（×）：从共享列表移除该论文，想删哪个删哪个
    const del = document.createElement("button");
    del.type = "button";
    del.className = "mat-del-btn";
    del.textContent = "×";
    del.title = "删除该论文";
    del.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();   // 阻止冒泡到 <label> 误触勾选
      if (!p.ready) {
        // 先使当前前端轮询失效；即便任务刚创建、taskId 尚未回传，后续也会被取消。
        p.prepareRunId = (p.prepareRunId || 0) + 1;
        if (p.prepareTaskId) {
          fetch("/api/stop_paper_prepare/" + p.prepareTaskId, { method: "POST" }).catch(() => {});
          p.prepareTaskId = "";
        }
      }
      const idx = convertedFiles.findIndex((c) => c.id === p.id);
      if (idx >= 0) convertedFiles.splice(idx, 1);
      matSelected.delete(p.id);
      // 注意：不再删除 matResults / matExpanded —— 删除来源文件时保留已提取报告（含展开状态）在原位
      if (typeof chatAttachPapers !== "undefined") chatAttachPapers.delete(p.id);
      renderMatFiles();
      renderAttachPanel();
      renderChips();
      // 提取进行中：完全不重绘「信息提取」栏，进度条与已提取报告块原样保留，由轮询循环继续更新进度条，做到零影响
      // 非提取态：重绘（已提取报告仍保留，因为 matResults 不再随删除清空）
      if (!matExtracting && !matColInfo.classList.contains("is-hidden")) renderMatReport(false);
    });
    item.appendChild(cb);
    item.appendChild(tag);
    item.appendChild(name);
    item.appendChild(del);
    matFileListEl.appendChild(item);
  });
}

matExtractBtn.addEventListener("click", async () => {
  if (matExtracting) { await stopMaterial(); return; }   // 提取中点按钮 = 停止（保留已提取论文）
  if (!matSelected.size) {
    alert("请先在左侧勾选至少一篇论文");
    return;
  }
  // 仅对「本模型尚未提取过」的论文发起提取；同篇换模型会新增一份结果，互不影响
  const provider = matModelSelect.value;
  const selected = [...matSelected].map((id) => convertedFiles.find((p) => p.id === id)).filter(Boolean);
  if (!selected.length) {
    matSelected.clear();
    renderMatFiles();
    matInfoBody.innerHTML = '<div class="mat-error">所选论文数据不存在，请重新选择</div>';
    return;
  }
  const pendingItems = selected.filter((p) => !p.ready);
  if (pendingItems.length) {
    alert("请等待所选 PDF 完成 Markdown 与 FigureIndex 预处理后再开始分析。");
    return;
  }
  const newItems = selected.filter((p) => !matResults.has(p.id + "::" + provider));
  if (!newItems.length) {
    alert("所选论文均已用当前模型提取过，无需重复提取（换其它模型可追加新结果）。");
    return;
  }
  matExtracting = true;
  const runToken = ++materialRunToken;
  materialCancelRequested = false;
  matExtractBtn.classList.add("is-busy");   // 配合 :hover 变红，显示「停止提取」
  // 确保第二栏可见，并先渲染「已提取论文（保留查看）+ 底部进度条」
  matColInfo.classList.remove("is-hidden");
  updateSplitters();
  renderMatReport(true);
  const newIds = newItems.map((p) => p.id);
  const payload = {
    papers: newItems.map((p) => ({
      id: p.id,
      name: p.name,
      document_id: p.serverDocumentId || p.id,
    })),
    provider: provider,
  };
  let data;
  try {
    const r = await fetch("/api/material_extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    data = await r.json().catch(() => ({}));
    if (!r.ok) {
      if (runToken !== materialRunToken || materialCancelRequested) return;
      matInfoBody.innerHTML = '<div class="mat-error">' + (data.error || "提取失败") + "</div>";
      resetExtractBtn();
      return;
    }
  } catch (e) {
    if (runToken !== materialRunToken || materialCancelRequested) return;
    matInfoBody.innerHTML = '<div class="mat-error">网络错误：' + e + "</div>";
    resetExtractBtn();
    return;
  }
  const tid = data.task_id;
  // 取消可能发生在 /api/material_extract 尚未返回 task_id 之前。
  if (runToken !== materialRunToken || materialCancelRequested) {
    await fetch("/api/stop_material/" + tid, { method: "POST" }).catch(() => {});
    return;
  }
  currentMaterialTaskId = tid;
  let reportedPaperCount = 0;
  while (true) {
    await sleep(700);
    if (runToken !== materialRunToken) return;
    // 轮询只属于当前 runToken；取消或重新提取后，旧任务响应直接丢弃。
    const pr = await fetch("/api/material_progress/" + tid);
    const p = await pr.json();
    if (runToken !== materialRunToken) return;
    const partialPapers = (p.result && p.result.papers) || [];
    if (partialPapers.length > reportedPaperCount) {
      partialPapers.slice(reportedPaperCount).forEach((paper, j) => {
        const id = newIds[reportedPaperCount + j];
        if (id != null) matResults.set(id + "::" + provider, paper);
      });
      reportedPaperCount = partialPapers.length;
      renderMatReport(!p.done);
    }
    // 只更新进度条块，不重建已提取论文块，避免影响查看
    const fill = document.getElementById("mat-prog-fill");
    const pct = document.getElementById("mat-loading-pct");
    const txt = document.getElementById("mat-loading-text");
    if (fill) fill.style.width = Math.min(100, Math.round(p.percent)) + "%";
    if (pct) pct.textContent = Math.round(p.percent) + "%";
    if (txt && p.stage) txt.textContent = p.stage;
    if (p.done) {
      if (p.error && !p.cancelled) {
        matInfoBody.innerHTML = '<div class="mat-error">' + p.error + "</div>";
        resetExtractBtn();
        return;
      }
      // 合并新结果到 matResults（键 = paperId::provider，支持同一篇存多模型结果）
      const papers = (p.result && p.result.papers) || [];
      papers.forEach((paper, j) => {
        if (newIds[j] != null) matResults.set(newIds[j] + "::" + provider, paper);
      });
      resetExtractBtn();
      renderMatReport(false);
      return;
    }
  }
});

// 提取按钮 hover：进行中时文案切换为「停止提取」（红色由 CSS .is-busy:hover 控制）
matExtractBtn.addEventListener("mouseenter", () => {
  if (matExtracting) matExtractBtn.textContent = "停止提取";
});
matExtractBtn.addEventListener("mouseleave", () => {
  if (matExtracting) matExtractBtn.textContent = "提取";
});

// 复位提取按钮到初始状态
function resetExtractBtn() {
  matExtracting = false;
  stopMaterialFlag = false;
  currentMaterialTaskId = null;
  matExtractBtn.classList.remove("is-busy");
  matExtractBtn.textContent = "提取";
}

/* ==================== 知识库：当前页面会话内的手工归档 ==================== */
// 后续接入数据库时，只需把本段的内存读写方法替换成 API 请求。
const knowledgeState = {
  folders: [],
  reports: new Map(),
  links: new Map(),
  expandedFolders: new Set(),
  selectedFolderId: "",
  selectedReportId: "",
  showAllFolderId: "",
  searchTerm: "",
  archiveReportId: "",
  archiveDraft: null,
  archiveSelectedFolderIds: new Set(),
  pendingArchiveReportId: "",
  pendingArchiveDraft: null,
  editingFolderId: "",
  deletingFolderId: "",
};

const knowledgeTreeEl = document.getElementById("knowledge-tree");
const knowledgeContentEl = document.getElementById("knowledge-content");
const knowledgeSearchEl = document.getElementById("knowledge-search");
const knowledgeLayoutEl = document.getElementById("knowledge-layout");
const knowledgeSplitterEl = document.getElementById("knowledge-splitter");
const knowledgeNewFolderIconBtn = document.getElementById("knowledge-new-folder-icon");
const knowledgeFolderDialog = document.getElementById("knowledge-folder-dialog");
const knowledgeFolderForm = document.getElementById("knowledge-folder-form");
const knowledgeFolderNameEl = document.getElementById("knowledge-folder-name");
const knowledgeFolderErrorEl = document.getElementById("knowledge-folder-error");
const knowledgeFolderTitleEl = document.getElementById("knowledge-folder-dialog-title");
const knowledgeFolderCopyEl = document.getElementById("knowledge-folder-dialog-copy");
const knowledgeFolderConfirmBtn = document.getElementById("knowledge-folder-confirm");
const knowledgeArchiveDialog = document.getElementById("knowledge-archive-dialog");
const knowledgeArchiveForm = document.getElementById("knowledge-archive-form");
const knowledgeArchiveSearchEl = document.getElementById("knowledge-archive-search");
const knowledgeArchiveListEl = document.getElementById("knowledge-archive-list");
const knowledgeArchiveEmptyEl = document.getElementById("knowledge-archive-empty");
const knowledgeArchiveCopyEl = document.getElementById("knowledge-archive-dialog-copy");
const knowledgeArchiveConfirmBtn = document.getElementById("knowledge-archive-confirm");
const knowledgeArchiveSelectionCountEl = document.getElementById("knowledge-archive-selection-count");
const knowledgeDeleteDialog = document.getElementById("knowledge-delete-dialog");
const knowledgeDeleteCopyEl = document.getElementById("knowledge-delete-dialog-copy");

async function loadKnowledgeState(render = false) {
  try {
    const response = await fetch("/api/knowledge");
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "知识库加载失败");
    knowledgeState.folders = data.folders || [];
    knowledgeState.reports = new Map((data.reports || []).map((report) => [report.id, report]));
    knowledgeState.links = new Map(knowledgeState.folders.map((folder) => [folder.id, new Map()]));
    (data.links || []).forEach((link) => {
      if (!knowledgeState.links.has(link.folderId)) knowledgeState.links.set(link.folderId, new Map());
      knowledgeState.links.get(link.folderId).set(link.reportId, link.createdAt || Date.now());
    });
    if (render) renderKnowledgeWorkspace();
    return true;
  } catch (error) {
    if (render) {
      knowledgeContentEl.innerHTML = `<div class="knowledge-empty-state"><h3>知识库暂时不可用</h3><p>${error.message}</p></div>`;
    }
    return false;
  }
}

function newKnowledgeId(prefix) {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

function normalizeKnowledgeName(value) {
  return String(value || "").trim().replace(/\s+/g, " ");
}

function folderById(folderId) {
  return knowledgeState.folders.find((folder) => folder.id === folderId) || null;
}

function reportById(reportId) {
  return knowledgeState.reports.get(reportId) || null;
}

function folderReportIds(folderId) {
  return [...(knowledgeState.links.get(folderId) || new Map()).entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([reportId]) => reportId)
    .filter((reportId) => knowledgeState.reports.has(reportId));
}

function foldersForReport(reportId) {
  return knowledgeState.folders.filter((folder) => (knowledgeState.links.get(folder.id) || new Map()).has(reportId));
}

function reportArchiveKey(reportId) {
  return `report:${reportId}`;
}

function archivedReportForSource(reportId) {
  return [...knowledgeState.reports.values()].find((report) =>
    report.sourceReportId === reportId || report.id === reportId
  ) || null;
}

function snapshotMaterialReport(reportId, paper) {
  const sourceId = String(reportId || "").split("::", 1)[0];
  const source = convertedFiles.find((item) => item.id === sourceId);
  return {
    id: reportArchiveKey(reportId),
    sourceReportId: reportId,
    name: paper.name || "（未命名论文）",
    model: paper.model || "",
    modelLabel: (paper.model && matModelLabels[paper.model]) || paper.model_label || paper.model || "未记录模型",
    generatedAt: paper.generated_at || "",
    elapsed: paper.elapsed || 0,
    part1: paper.part1 || "",
    part2: paper.part2 || "",
    part3: paper.part3 || "",
    parts: paper.parts || {},
    documentId: source && source.serverDocumentId ? source.serverDocumentId : "",
    figureIndex: source && source.figureIndex ? source.figureIndex : [],
    createdAt: Date.now(),
  };
}

function ensureKnowledgeReport(reportId, paper) {
  const key = reportArchiveKey(reportId);
  let report = reportById(key);
  if (!report) {
    report = snapshotMaterialReport(reportId, paper);
    knowledgeState.reports.set(key, report);
  }
  return report;
}

function setReportFolders(report, folderIds) {
  const desired = new Set(folderIds);
  knowledgeState.folders.forEach((folder) => {
    let links = knowledgeState.links.get(folder.id);
    if (!links) {
      links = new Map();
      knowledgeState.links.set(folder.id, links);
    }
    if (desired.has(folder.id)) links.set(report.id, Date.now());
    else links.delete(report.id);
  });
}

function formatKnowledgeDate(timestamp) {
  if (!timestamp) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(new Date(timestamp));
}

function closeDialog(dialog) {
  if (dialog && dialog.open) dialog.close();
}

function activateKnowledgeWorkspace() {
  document.getElementById("nav-knowledge").click();
}

function openKnowledgeFolderDialog(folder = null) {
  knowledgeState.editingFolderId = folder ? folder.id : "";
  knowledgeFolderTitleEl.textContent = folder ? "重命名归档文件夹" : "新建归档文件夹";
  knowledgeFolderCopyEl.textContent = folder ? "修改后会立即同步到知识库目录。" : "文件夹中的报告仅由你主动归档。";
  knowledgeFolderConfirmBtn.textContent = folder ? "保存修改" : "创建文件夹";
  knowledgeFolderNameEl.value = folder ? folder.name : "";
  knowledgeFolderErrorEl.textContent = "";
  knowledgeFolderErrorEl.classList.add("is-hidden");
  if (!knowledgeFolderDialog.open) knowledgeFolderDialog.showModal();
  setTimeout(() => knowledgeFolderNameEl.focus(), 0);
}

function closeKnowledgeFolderDialog() {
  closeDialog(knowledgeFolderDialog);
  knowledgeState.editingFolderId = "";
}

function openKnowledgeArchiveDialog(reportId, paper) {
  if (!paper) return;
  const existing = archivedReportForSource(reportId);
  const report = paper.id === reportArchiveKey(reportId)
    ? paper
    : snapshotMaterialReport(reportId, paper);
  if (existing) {
    report.id = existing.id;
    report.createdAt = existing.createdAt;
  }
  knowledgeState.archiveReportId = report.id;
  knowledgeState.archiveDraft = report;
  knowledgeState.archiveSelectedFolderIds = new Set(
    foldersForReport(existing ? existing.id : report.id).map((folder) => folder.id),
  );
  knowledgeArchiveSearchEl.value = "";
  knowledgeArchiveCopyEl.textContent = `选择“${report.name}”要归入的文件夹。`;
  renderKnowledgeArchiveChoices();
  if (!knowledgeArchiveDialog.open) knowledgeArchiveDialog.showModal();
}

function closeKnowledgeArchiveDialog() {
  closeDialog(knowledgeArchiveDialog);
  knowledgeState.archiveReportId = "";
  knowledgeState.archiveDraft = null;
  knowledgeState.archiveSelectedFolderIds = new Set();
}

function renderKnowledgeArchiveChoices() {
  const report = reportById(knowledgeState.archiveReportId) || knowledgeState.archiveDraft;
  const query = normalizeKnowledgeName(knowledgeArchiveSearchEl.value).toLocaleLowerCase();
  const selectedFolderIds = knowledgeState.archiveSelectedFolderIds;
  const visibleFolders = knowledgeState.folders.filter((folder) => !query || folder.name.toLocaleLowerCase().includes(query));
  knowledgeArchiveListEl.innerHTML = "";
  knowledgeArchiveEmptyEl.classList.toggle("is-hidden", knowledgeState.folders.length > 0);
  knowledgeArchiveListEl.classList.toggle("is-hidden", knowledgeState.folders.length === 0);

  visibleFolders.forEach((folder) => {
    const label = document.createElement("label");
    label.className = "knowledge-archive-option";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = folder.id;
    input.checked = selectedFolderIds.has(folder.id);
    input.addEventListener("change", () => {
      if (input.checked) knowledgeState.archiveSelectedFolderIds.add(folder.id);
      else knowledgeState.archiveSelectedFolderIds.delete(folder.id);
      updateKnowledgeArchiveConfirmState();
    });
    const copy = document.createElement("span");
    copy.className = "knowledge-archive-option-copy";
    const title = document.createElement("span");
    title.className = "knowledge-archive-option-title";
    title.textContent = folder.name;
    const meta = document.createElement("span");
    meta.className = "knowledge-archive-option-meta";
    meta.textContent = `${folderReportIds(folder.id).length} 篇报告`;
    copy.append(title, meta);
    label.append(input, copy);
    knowledgeArchiveListEl.appendChild(label);
  });
  if (knowledgeState.folders.length && !visibleFolders.length) {
    const empty = document.createElement("div");
    empty.className = "knowledge-inline-empty";
    empty.textContent = "没有匹配的归档文件夹。";
    knowledgeArchiveListEl.appendChild(empty);
  }
  updateKnowledgeArchiveConfirmState();
}

function updateKnowledgeArchiveConfirmState() {
  const count = knowledgeState.archiveSelectedFolderIds.size;
  const report = reportById(knowledgeState.archiveReportId) || knowledgeState.archiveDraft;
  const hasExistingMembership = report && foldersForReport(report.id).length > 0;
  knowledgeArchiveSelectionCountEl.textContent = `已选择 ${count} 个文件夹`;
  knowledgeArchiveConfirmBtn.disabled = count === 0 && !hasExistingMembership;
  knowledgeArchiveConfirmBtn.textContent = hasExistingMembership ? "保存归档" : "确认归档";
}

function openKnowledgeDeleteDialog(folder) {
  if (!folder) return;
  knowledgeState.deletingFolderId = folder.id;
  const count = folderReportIds(folder.id).length;
  knowledgeDeleteCopyEl.textContent = `删除“${folder.name}”会移除其中 ${count} 条归档关系；只存在于该文件夹的报告及其文章资产也会被删除。`;
  if (!knowledgeDeleteDialog.open) knowledgeDeleteDialog.showModal();
}

function closeKnowledgeDeleteDialog() {
  closeDialog(knowledgeDeleteDialog);
  knowledgeState.deletingFolderId = "";
}

async function removeReportFromKnowledgeFolder(folderId, reportId) {
  const response = await fetch(
    `/api/knowledge/folders/${encodeURIComponent(folderId)}/reports/${encodeURIComponent(reportId)}`,
    { method: "DELETE" },
  );
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    alert(data.error || "移除归档失败");
    return;
  }
  await loadKnowledgeState(true);
}

function renderKnowledgeTree() {
  knowledgeTreeEl.innerHTML = "";
  const query = normalizeKnowledgeName(knowledgeState.searchTerm).toLocaleLowerCase();
  if (!knowledgeState.folders.length) {
    const empty = document.createElement("div");
    empty.className = "knowledge-tree-empty";
    empty.textContent = "还没有归档文件夹。";
    knowledgeTreeEl.appendChild(empty);
    return;
  }
  const matchedFolders = knowledgeState.folders.filter((folder) => {
    const reports = folderReportIds(folder.id).map(reportById).filter(Boolean);
    return !query || folder.name.toLocaleLowerCase().includes(query) || reports.some((report) => report.name.toLocaleLowerCase().includes(query));
  });
  if (!matchedFolders.length) {
    const empty = document.createElement("div");
    empty.className = "knowledge-tree-empty";
    empty.textContent = "没有匹配的文件夹或论文。";
    knowledgeTreeEl.appendChild(empty);
    return;
  }

  matchedFolders.forEach((folder) => {
    const reportIds = folderReportIds(folder.id);
    const reports = reportIds.map(reportById).filter(Boolean);
    const queryMatchesReports = query ? reports.filter((report) => report.name.toLocaleLowerCase().includes(query)) : reports;
    const isExpanded = knowledgeState.expandedFolders.has(folder.id) || Boolean(query && queryMatchesReports.length);
    const row = document.createElement("div");
    row.className = "knowledge-folder-row" + (knowledgeState.selectedFolderId === folder.id ? " is-selected" : "");

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "knowledge-folder-toggle";
    toggle.textContent = isExpanded ? "▾" : "▸";
    toggle.title = isExpanded ? "收起报告" : "展开报告";
    toggle.addEventListener("click", () => {
      if (knowledgeState.expandedFolders.has(folder.id)) knowledgeState.expandedFolders.delete(folder.id);
      else knowledgeState.expandedFolders.add(folder.id);
      renderKnowledgeTree();
    });

    const folderButton = document.createElement("button");
    folderButton.type = "button";
    folderButton.className = "knowledge-folder-button";
    const name = document.createElement("span");
    name.className = "knowledge-folder-name";
    name.textContent = folder.name;
    name.title = folder.name;
    const count = document.createElement("span");
    count.className = "knowledge-folder-count";
    count.textContent = reportIds.length;
    folderButton.append(name, count);
    folderButton.addEventListener("click", () => {
      knowledgeState.selectedFolderId = folder.id;
      knowledgeState.selectedReportId = "";
      knowledgeState.showAllFolderId = "";
      renderKnowledgeWorkspace();
    });

    const menu = document.createElement("details");
    menu.className = "knowledge-folder-menu";
    const summary = document.createElement("summary");
    summary.title = "文件夹操作";
    summary.setAttribute("aria-label", "文件夹操作");
    summary.textContent = "⋯";
    const menuBody = document.createElement("div");
    menuBody.className = "knowledge-folder-menu-body";
    const rename = document.createElement("button");
    rename.type = "button";
    rename.textContent = "重命名";
    rename.addEventListener("click", () => { menu.open = false; openKnowledgeFolderDialog(folder); });
    const manage = document.createElement("button");
    manage.type = "button";
    manage.textContent = "管理论文";
    manage.addEventListener("click", () => {
      menu.open = false;
      knowledgeState.selectedFolderId = folder.id;
      knowledgeState.selectedReportId = "";
      knowledgeState.showAllFolderId = folder.id;
      renderKnowledgeWorkspace();
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "knowledge-menu-danger";
    remove.textContent = "删除文件夹";
    remove.addEventListener("click", () => { menu.open = false; openKnowledgeDeleteDialog(folder); });
    menuBody.append(rename, manage, remove);
    menu.append(summary, menuBody);
    row.append(toggle, folderButton, menu);
    knowledgeTreeEl.appendChild(row);

    if (!isExpanded) return;
    const reportList = document.createElement("div");
    reportList.className = "knowledge-report-tree";
    const shownReports = query ? queryMatchesReports : reports.slice(0, 10);
    shownReports.forEach((report) => {
      const reportButton = document.createElement("button");
      reportButton.type = "button";
      reportButton.className = "knowledge-report-tree-item" +
        (knowledgeState.selectedReportId === report.id && knowledgeState.selectedFolderId === folder.id ? " is-selected" : "");
      reportButton.textContent = report.name;
      reportButton.title = report.name;
      reportButton.addEventListener("click", () => {
        knowledgeState.selectedFolderId = folder.id;
        knowledgeState.selectedReportId = report.id;
        knowledgeState.showAllFolderId = "";
        renderKnowledgeWorkspace();
      });
      reportList.appendChild(reportButton);
    });
    if (!query && reports.length > 10) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "knowledge-view-all";
      more.textContent = `查看全部 ${reports.length} 篇`;
      more.addEventListener("click", () => {
        knowledgeState.selectedFolderId = folder.id;
        knowledgeState.selectedReportId = "";
        knowledgeState.showAllFolderId = folder.id;
        renderKnowledgeWorkspace();
      });
      reportList.appendChild(more);
    }
    if (!reports.length) {
      const empty = document.createElement("div");
      empty.className = "knowledge-folder-empty";
      empty.textContent = "暂无归档报告";
      reportList.appendChild(empty);
    }
    knowledgeTreeEl.appendChild(reportList);
  });
}

function createKnowledgeBreadcrumb(items) {
  const nav = document.createElement("nav");
  nav.className = "knowledge-breadcrumb";
  nav.setAttribute("aria-label", "当前位置");
  items.forEach((item, index) => {
    if (index) {
      const separator = document.createElement("span");
      separator.className = "knowledge-breadcrumb-separator";
      separator.textContent = "/";
      nav.appendChild(separator);
    }
    const part = document.createElement(item.onClick ? "button" : "span");
    if (item.onClick) {
      part.type = "button";
      part.addEventListener("click", item.onClick);
    }
    part.className = "knowledge-breadcrumb-part" + (item.current ? " is-current" : "");
    part.textContent = item.label;
    part.title = item.label;
    nav.appendChild(part);
  });
  return nav;
}

function createKnowledgeEmptyState() {
  const empty = document.createElement("div");
  empty.className = "knowledge-content-empty";
  const title = document.createElement("h2");
  title.textContent = "从一个归档文件夹开始";
  const copy = document.createElement("p");
  copy.textContent = "知识库只显示你主动归档的分析报告。";
  const action = document.createElement("button");
  action.type = "button";
  action.className = "knowledge-empty-action";
  action.textContent = "新建文件夹";
  action.addEventListener("click", () => openKnowledgeFolderDialog());
  empty.append(title, copy, action);
  return empty;
}

function createKnowledgeReportListItem(folder, report) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = "knowledge-report-list-item";
  const main = document.createElement("span");
  main.className = "knowledge-report-list-main";
  const title = document.createElement("span");
  title.className = "knowledge-report-list-title";
  title.textContent = report.name;
  title.title = report.name;
  const meta = document.createElement("span");
  meta.className = "knowledge-report-list-meta";
  meta.textContent = `${report.modelLabel} · ${report.generatedAt || "未记录生成时间"}`;
  main.append(title, meta);
  const arrow = document.createElement("span");
  arrow.className = "knowledge-report-list-arrow";
  arrow.textContent = "›";
  item.append(main, arrow);
  item.addEventListener("click", () => {
    knowledgeState.selectedFolderId = folder.id;
    knowledgeState.selectedReportId = report.id;
    knowledgeState.showAllFolderId = "";
    renderKnowledgeWorkspace();
  });
  return item;
}

function createKnowledgeFolderOverview(folder) {
  const wrapper = document.createElement("div");
  wrapper.className = "knowledge-overview";
  wrapper.appendChild(createKnowledgeBreadcrumb([
    { label: "知识库", onClick: () => { knowledgeState.selectedFolderId = ""; renderKnowledgeWorkspace(); } },
    { label: folder.name, current: true },
  ]));
  const heading = document.createElement("div");
  heading.className = "knowledge-content-heading";
  const titleWrap = document.createElement("div");
  const title = document.createElement("h2");
  title.textContent = folder.name;
  const meta = document.createElement("p");
  const count = folderReportIds(folder.id).length;
  meta.textContent = `${count} 篇归档报告 · 创建于 ${formatKnowledgeDate(folder.createdAt)}`;
  titleWrap.append(title, meta);
  const manage = document.createElement("button");
  manage.type = "button";
  manage.className = "knowledge-manage-btn";
  manage.textContent = "管理论文";
  manage.addEventListener("click", () => {
    knowledgeState.showAllFolderId = folder.id;
    renderKnowledgeWorkspace();
  });
  heading.append(titleWrap, manage);
  wrapper.appendChild(heading);

  if (!count) {
    const empty = document.createElement("div");
    empty.className = "knowledge-folder-overview-empty";
    empty.textContent = "这个文件夹还没有归档报告。请在信息提取区生成报告后点击“归档”。";
    wrapper.appendChild(empty);
    return wrapper;
  }
  const sectionTitle = document.createElement("h3");
  sectionTitle.className = "knowledge-section-heading";
  sectionTitle.textContent = "最近归档";
  wrapper.appendChild(sectionTitle);
  const list = document.createElement("div");
  list.className = "knowledge-overview-list";
  folderReportIds(folder.id).slice(0, 5).map(reportById).filter(Boolean).forEach((report) => {
    list.appendChild(createKnowledgeReportListItem(folder, report));
  });
  wrapper.appendChild(list);
  return wrapper;
}

function createKnowledgeFolderAllReports(folder) {
  const wrapper = document.createElement("div");
  wrapper.className = "knowledge-overview";
  wrapper.appendChild(createKnowledgeBreadcrumb([
    { label: "知识库", onClick: () => { knowledgeState.selectedFolderId = ""; knowledgeState.showAllFolderId = ""; renderKnowledgeWorkspace(); } },
    { label: folder.name, onClick: () => { knowledgeState.showAllFolderId = ""; renderKnowledgeWorkspace(); } },
    { label: "全部报告", current: true },
  ]));
  const heading = document.createElement("div");
  heading.className = "knowledge-content-heading";
  const copy = document.createElement("div");
  const title = document.createElement("h2");
  title.textContent = `${folder.name} · 全部报告`;
  const meta = document.createElement("p");
  meta.textContent = `${folderReportIds(folder.id).length} 篇归档报告`;
  copy.append(title, meta);
  heading.appendChild(copy);
  wrapper.appendChild(heading);
  const filter = document.createElement("input");
  filter.type = "search";
  filter.className = "knowledge-list-search";
  filter.placeholder = "搜索当前文件夹中的论文";
  wrapper.appendChild(filter);
  const list = document.createElement("div");
  list.className = "knowledge-overview-list knowledge-full-list";
  const renderList = () => {
    const query = normalizeKnowledgeName(filter.value).toLocaleLowerCase();
    list.innerHTML = "";
    const reports = folderReportIds(folder.id)
      .map(reportById)
      .filter((report) => report && (!query || report.name.toLocaleLowerCase().includes(query)));
    if (!reports.length) {
      const empty = document.createElement("div");
      empty.className = "knowledge-inline-empty";
      empty.textContent = "没有匹配的报告。";
      list.appendChild(empty);
      return;
    }
    reports.forEach((report) => list.appendChild(createKnowledgeReportListItem(folder, report)));
  };
  filter.addEventListener("input", renderList);
  renderList();
  wrapper.appendChild(list);
  return wrapper;
}

function createKnowledgeReportDetail(folder, report) {
  const wrapper = document.createElement("article");
  wrapper.className = "knowledge-report-detail";
  wrapper.appendChild(createKnowledgeBreadcrumb([
    { label: "知识库", onClick: () => { knowledgeState.selectedFolderId = ""; knowledgeState.selectedReportId = ""; renderKnowledgeWorkspace(); } },
    { label: folder.name, onClick: () => { knowledgeState.selectedReportId = ""; renderKnowledgeWorkspace(); } },
    { label: report.name, current: true },
  ]));
  const heading = document.createElement("div");
  heading.className = "knowledge-report-heading";
  const copy = document.createElement("div");
  const title = document.createElement("h2");
  title.textContent = report.name;
  const meta = document.createElement("p");
  meta.textContent = `${report.modelLabel} · ${report.generatedAt || "未记录生成时间"}`;
  copy.append(title, meta);
  const actions = document.createElement("div");
  actions.className = "knowledge-report-actions";
  const manage = document.createElement("button");
  manage.type = "button";
  manage.className = "knowledge-manage-btn";
  manage.textContent = "管理归档";
  manage.addEventListener("click", () => openKnowledgeArchiveDialog(report.sourceReportId, report));
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "knowledge-remove-btn";
  remove.textContent = "从当前文件夹移除";
  remove.addEventListener("click", () => {
    if (confirm(`从“${folder.name}”移除这份报告？不会删除报告内容或其他文件夹中的归档。`)) {
      removeReportFromKnowledgeFolder(folder.id, report.id);
    }
  });
  actions.append(manage, remove);
  heading.append(copy, actions);
  wrapper.appendChild(heading);

  const placements = document.createElement("div");
  placements.className = "knowledge-report-placements";
  const placementLabel = document.createElement("span");
  placementLabel.textContent = "已归档至";
  placements.appendChild(placementLabel);
  foldersForReport(report.id).forEach((linkedFolder) => {
    const tag = document.createElement("button");
    tag.type = "button";
    tag.className = "knowledge-folder-tag";
    tag.textContent = linkedFolder.name;
    tag.addEventListener("click", () => {
      knowledgeState.selectedFolderId = linkedFolder.id;
      knowledgeState.selectedReportId = "";
      renderKnowledgeWorkspace();
    });
    placements.appendChild(tag);
  });
  wrapper.appendChild(placements);

  const content = document.createElement("div");
  content.className = "knowledge-report-content";
  const main = document.createElement("div");
  main.className = "knowledge-report-main";
  const figureMap = figureMapFromIndex(report.figureIndex);
  const figurePreview = createFigurePreviewPanel(figureMap);
  const sections = [
    { content: report.part1, strip: true },
    { content: report.part2, strip: false },
    { content: report.part3, strip: true, compact: true },
  ];
  sections.forEach((section) => {
    const markdown = extractedReportMarkdown(section.content, report.name, section.strip);
    if (!markdown) return;
    const block = document.createElement("section");
    block.className = "report-sec";
    const body = document.createElement("div");
    body.className = "report-sec-body preview-md" + (section.compact ? " report-sec-body-conclusion" : "");
    renderReportMarkdown(body, markdown, figureMap, figurePreview.show);
    block.appendChild(body);
    main.appendChild(block);
  });
  content.append(main, figurePreview.panel);
  wrapper.appendChild(content);
  return wrapper;
}

function renderKnowledgeContent() {
  knowledgeContentEl.innerHTML = "";
  if (!knowledgeState.folders.length) {
    knowledgeContentEl.appendChild(createKnowledgeEmptyState());
    return;
  }
  const folder = folderById(knowledgeState.selectedFolderId);
  if (!folder) {
    const landing = document.createElement("div");
    landing.className = "knowledge-content-empty";
    const title = document.createElement("h2");
    title.textContent = "选择一个归档文件夹";
    const copy = document.createElement("p");
    copy.textContent = "在左侧展开文件夹并打开其中的分析报告。";
    landing.append(title, copy);
    knowledgeContentEl.appendChild(landing);
    return;
  }
  const report = reportById(knowledgeState.selectedReportId);
  if (report && folderReportIds(folder.id).includes(report.id)) {
    knowledgeContentEl.appendChild(createKnowledgeReportDetail(folder, report));
  } else if (knowledgeState.showAllFolderId === folder.id) {
    knowledgeContentEl.appendChild(createKnowledgeFolderAllReports(folder));
  } else {
    knowledgeContentEl.appendChild(createKnowledgeFolderOverview(folder));
  }
}

function renderKnowledgeWorkspace() {
  renderKnowledgeTree();
  renderKnowledgeContent();
}

function enableKnowledgeSplitter() {
  const savedWidth = Number(sessionStorage.getItem("knowledge-tree-width"));
  if (savedWidth >= 220 && savedWidth <= 480) {
    knowledgeLayoutEl.style.setProperty("--knowledge-tree-width", `${savedWidth}px`);
  }
  knowledgeSplitterEl.addEventListener("mousedown", (event) => {
    event.preventDefault();
    knowledgeSplitterEl.classList.add("dragging");
    const startX = event.clientX;
    const startWidth = document.querySelector(".knowledge-tree-panel").getBoundingClientRect().width;
    const onMove = (moveEvent) => {
      const width = Math.max(220, Math.min(480, startWidth + moveEvent.clientX - startX));
      knowledgeLayoutEl.style.setProperty("--knowledge-tree-width", `${Math.round(width)}px`);
    };
    const onUp = () => {
      const width = Math.round(document.querySelector(".knowledge-tree-panel").getBoundingClientRect().width);
      sessionStorage.setItem("knowledge-tree-width", String(width));
      knowledgeSplitterEl.classList.remove("dragging");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.userSelect = "";
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    document.body.style.userSelect = "none";
  });
}

knowledgeNewFolderIconBtn.addEventListener("click", () => openKnowledgeFolderDialog());
knowledgeSearchEl.addEventListener("input", () => {
  knowledgeState.searchTerm = knowledgeSearchEl.value;
  renderKnowledgeTree();
});
knowledgeFolderForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = normalizeKnowledgeName(knowledgeFolderNameEl.value);
  const duplicate = knowledgeState.folders.some((folder) =>
    folder.name.toLocaleLowerCase() === name.toLocaleLowerCase() && folder.id !== knowledgeState.editingFolderId
  );
  if (!name) {
    knowledgeFolderErrorEl.textContent = "请输入文件夹名称。";
    knowledgeFolderErrorEl.classList.remove("is-hidden");
    return;
  }
  if (duplicate) {
    knowledgeFolderErrorEl.textContent = "已存在同名归档文件夹。";
    knowledgeFolderErrorEl.classList.remove("is-hidden");
    return;
  }
  const editingId = knowledgeState.editingFolderId;
  const response = await fetch(
    editingId ? `/api/knowledge/folders/${encodeURIComponent(editingId)}` : "/api/knowledge/folders",
    {
      method: editingId ? "PATCH" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    },
  );
  const saved = await response.json().catch(() => ({}));
  if (!response.ok) {
    knowledgeFolderErrorEl.textContent = saved.error || "文件夹保存失败";
    knowledgeFolderErrorEl.classList.remove("is-hidden");
    return;
  }
  knowledgeState.selectedFolderId = saved.id;
  knowledgeState.selectedReportId = "";
  closeKnowledgeFolderDialog();
  await loadKnowledgeState(true);
  const pendingReport = reportById(knowledgeState.pendingArchiveReportId) || knowledgeState.pendingArchiveDraft;
  knowledgeState.pendingArchiveReportId = "";
  knowledgeState.pendingArchiveDraft = null;
  if (pendingReport) openKnowledgeArchiveDialog(pendingReport.sourceReportId, pendingReport);
});
knowledgeArchiveSearchEl.addEventListener("input", renderKnowledgeArchiveChoices);
knowledgeArchiveForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const report = reportById(knowledgeState.archiveReportId) || knowledgeState.archiveDraft;
  if (!report) return;
  const selectedIds = [...knowledgeState.archiveSelectedFolderIds];
  if (!selectedIds.length) return;
  if (!report.documentId) {
    alert("该报告没有可归档的临时文档，请重新添加原始 PDF 并完成预处理。");
    return;
  }
  const submitArchive = async (updateExisting) => {
    const response = await fetch("/api/knowledge/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        document_id: report.documentId,
        folder_ids: selectedIds,
        report,
        update_existing: updateExisting,
      }),
    });
    const data = await response.json().catch(() => ({}));
    return { response, data };
  };
  let { response, data } = await submitArchive(false);
  if (response.status === 409 && data.conflict) {
    const confirmed = window.confirm(
      "该文章已经有一份归档分析报告。是否按部分合并更新？本次失败、取消或未选择的部分会保留旧值。",
    );
    if (!confirmed) return;
    ({ response, data } = await submitArchive(true));
  }
  if (!response.ok) {
    alert(data.error || "归档失败");
    return;
  }
  knowledgeState.selectedFolderId = selectedIds[0] || "";
  knowledgeState.selectedReportId = data.report && data.report.id || "";
  knowledgeState.showAllFolderId = "";
  closeKnowledgeArchiveDialog();
  await loadKnowledgeState(true);
  renderMatReport(false);
});
document.getElementById("knowledge-folder-cancel").addEventListener("click", closeKnowledgeFolderDialog);
document.getElementById("knowledge-folder-cancel-icon").addEventListener("click", closeKnowledgeFolderDialog);
document.getElementById("knowledge-archive-cancel").addEventListener("click", closeKnowledgeArchiveDialog);
document.getElementById("knowledge-archive-cancel-icon").addEventListener("click", closeKnowledgeArchiveDialog);
document.getElementById("knowledge-archive-go-create").addEventListener("click", () => {
  knowledgeState.pendingArchiveReportId = knowledgeState.archiveReportId;
  knowledgeState.pendingArchiveDraft = knowledgeState.archiveDraft;
  closeKnowledgeArchiveDialog();
  activateKnowledgeWorkspace();
  openKnowledgeFolderDialog();
});
document.getElementById("knowledge-delete-cancel").addEventListener("click", closeKnowledgeDeleteDialog);
document.getElementById("knowledge-delete-cancel-icon").addEventListener("click", closeKnowledgeDeleteDialog);
document.getElementById("knowledge-delete-confirm").addEventListener("click", async () => {
  const folder = folderById(knowledgeState.deletingFolderId);
  if (!folder) return;
  const response = await fetch(`/api/knowledge/folders/${encodeURIComponent(folder.id)}`, {
    method: "DELETE",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    alert(data.error || "删除文件夹失败");
    return;
  }
  if (knowledgeState.selectedFolderId === folder.id) {
    knowledgeState.selectedFolderId = "";
    knowledgeState.selectedReportId = "";
    knowledgeState.showAllFolderId = "";
  }
  closeKnowledgeDeleteDialog();
  await loadKnowledgeState(true);
});
[knowledgeFolderDialog, knowledgeArchiveDialog, knowledgeDeleteDialog].forEach((dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) closeDialog(dialog);
  });
});
enableKnowledgeSplitter();

// 停止提取：通知后端 terminate 子进程（立即取消），UI 立即复位；主轮询继续轮询到
// done 后把后端已回传的已完成篇合并进报告（满足"已处理好的显示，没处理好的停下"）
async function stopMaterial() {
  stopMaterialFlag = true;
  materialCancelRequested = true;
  materialRunToken += 1;
  const taskId = currentMaterialTaskId;
  if (taskId) {
    await fetch("/api/stop_material/" + taskId, { method: "POST" }).catch(() => {});
  }
  resetExtractBtn();
  renderMatReport(false);   // 仅保留已提取论文块，移除底部进度条
}

// 去掉每段开头冗余的「### 论文文件名」标题（块头已显示文件名；仅 part1/part3 含此前缀）
function stripLeadingName(md, name) {
  if (!md) return md;
  const esc = String(name).replace(/[.*+?^${}()|[\\]\\]/g, "\\$&");
  return md.replace(new RegExp("^###\\s+" + esc + "\\s*\\n+"), "");
}

// 仅渲染实际已生成的内容；未提取/未生成的模块不占用页面空间。
function extractedReportMarkdown(content, name, strip) {
  let markdown = typeof content === "string" ? content.trim() : "";
  if (!markdown) return "";
  if (/^[（(]\s*未(?:生成|提取)/.test(markdown)) return "";
  if (strip) markdown = stripLeadingName(markdown, name).trim();
  return markdown;
}

// 从 matResults 渲染报告：已提取论文逐篇可展开/收回；isExtracting 时在底部追加进度条
function renderMatReport(isExtracting) {
  matInfoBody.innerHTML = "";
  const report = document.createElement("div");
  report.className = "report";

  const count = matResults.size;

  // 无已提取论文且非提取中：占位提示
  if (count === 0 && !isExtracting) {
    const ph = document.createElement("div");
    ph.className = "mat-info-placeholder";
    ph.textContent = "在左侧勾选一篇或多篇论文，点击右上角「提取」。";
    report.appendChild(ph);
    matInfoBody.appendChild(report);
    matExportBtn.classList.add("is-hidden");
    updateSplitters();
    return;
  }

  // 每篇论文一个可单独展开/收回的报告块（多篇时点开一篇自动收起其它篇）
  const renderOnePaper = (paper, id) => {
    const block = document.createElement("div");
    block.className = "report-paper";
    block.dataset.paperId = id;

    const phead = document.createElement("div");
    phead.className = "report-paper-head";
    const modelLabel = (paper.model && matModelLabels[paper.model]) || paper.model_label || paper.model || "";
    const expandBtn = document.createElement("button");
    expandBtn.type = "button";
    expandBtn.className = "report-paper-open";
    const toggle = document.createElement("span");
    toggle.className = "report-paper-toggle";
    const reportName = document.createElement("span");
    reportName.className = "report-paper-name";
    reportName.textContent = paper.name || "（未命名论文）";
    expandBtn.append(toggle, reportName);
    phead.appendChild(expandBtn);
    if (modelLabel) {
      const ml = document.createElement("span");
      ml.className = "report-paper-model";
      ml.textContent = "· " + modelLabel;
      expandBtn.appendChild(ml);
    }
    const archiveBtn = document.createElement("button");
    archiveBtn.type = "button";
    const existingArchive = foldersForReport(reportArchiveKey(id));
    archiveBtn.className = "report-paper-archive" + (existingArchive.length ? " is-archived" : "");
    archiveBtn.textContent = existingArchive.length ? "已归档" : "归档";
    archiveBtn.title = existingArchive.length ? "管理归档文件夹" : "归档到知识库";
    archiveBtn.addEventListener("click", () => openKnowledgeArchiveDialog(id, paper));
    phead.appendChild(archiveBtn);

    const pbody = document.createElement("div");
    const expanded = matExpanded.has(id);
    pbody.className = "report-paper-body" + (expanded ? " is-expanded" : " is-hidden");
    toggle.textContent = expanded ? "▾" : "▸";

    const figureMap = figureMapForReport(id);
    const figurePreview = createFigurePreviewPanel(figureMap);
    const content = document.createElement("div");
    content.className = "report-paper-content";
    const main = document.createElement("div");
    main.className = "report-paper-main preview-md";

    // 分析报告大标题 + 生成时间 / 耗时
    const paperHeader = document.createElement("div");
    paperHeader.className = "paper-header";
    const paperTitle = document.createElement("div");
    paperTitle.className = "paper-header-title";
    paperTitle.textContent = "分析报告";
    const paperMeta = document.createElement("div");
    paperMeta.className = "paper-header-meta";
    const elapsed = paper.elapsed != null ? paper.elapsed : 0;
    const mm = Math.floor(elapsed / 60);
    const ss = elapsed % 60;
    const elapsedStr = mm > 0 ? mm + "m" + ss + "s" : ss + "s";
    const genAt = paper.generated_at || "";
    paperMeta.textContent = genAt ? genAt + " · " + elapsedStr : elapsedStr;
    paperHeader.appendChild(paperTitle);
    paperHeader.appendChild(paperMeta);
    main.appendChild(paperHeader);

    const sections = [
      { content: paper.part1, strip: true },
      { content: paper.part2, strip: false },
      { content: paper.part3, strip: true, compact: true },
    ];
    sections.forEach((section) => {
      const markdown = extractedReportMarkdown(section.content, paper.name, section.strip);
      if (!markdown) return;
      const sec = document.createElement("section");
      sec.className = "report-sec";
      const body = document.createElement("div");
      body.className = "report-sec-body preview-md" + (section.compact ? " report-sec-body-conclusion" : "");
      renderReportMarkdown(body, markdown, figureMap, figurePreview.show);
      sec.appendChild(body);
      main.appendChild(sec);
    });

    content.appendChild(main);
    content.appendChild(figurePreview.panel);
    pbody.appendChild(content);

    expandBtn.addEventListener("click", () => {
      const willExpand = pbody.classList.contains("is-hidden");
      if (willExpand) {
        // 收起其它已展开篇，并同步 matExpanded
        report.querySelectorAll(".report-paper").forEach((blk) => {
          const bid = blk.dataset.paperId;
          if (bid && bid !== id) matExpanded.delete(bid);
          const bb = blk.querySelector(".report-paper-body");
          const bh = blk.querySelector(".report-paper-head");
          if (bb) { bb.classList.remove("is-expanded"); bb.classList.add("is-hidden"); }
          if (bh) bh.querySelector(".report-paper-toggle").textContent = "▸";
        });
        pbody.classList.remove("is-hidden");
        pbody.classList.add("is-expanded");
        toggle.textContent = "▾";
        matExpanded.add(id);
      } else {
        pbody.classList.remove("is-expanded");
        pbody.classList.add("is-hidden");
        toggle.textContent = "▸";
        matExpanded.delete(id);
      }
    });

    block.appendChild(phead);
    block.appendChild(pbody);
    return block;
  };

  // 已提取论文块（保留顺序与展开状态）
  [...matResults.entries()].forEach(([id, paper]) => report.appendChild(renderOnePaper(paper, id)));

  // 提取中：在已提取论文下方追加进度条块（仅更新其进度，不重建上方论文块）
  if (isExtracting) {
    const loading = document.createElement("div");
    loading.className = "mat-loading";
    loading.innerHTML =
      '<div class="mat-loading-text" id="mat-loading-text">论文信息提取中…</div>' +
      '<div class="prog-track" style="width:100%"><div class="prog-fill" id="mat-prog-fill" style="width:0"></div></div>' +
      '<div class="mat-loading-pct" id="mat-loading-pct">0%</div>';
    report.appendChild(loading);
  }

  matInfoBody.appendChild(report);
  matExportBtn.classList.remove("is-hidden");
  updateSplitters();
}

function reportExportMeta(paper) {
  const modelLabel = (paper.model && matModelLabels[paper.model]) || paper.model_label || paper.model || "未记录模型";
  return [modelLabel, paper.generated_at || "未记录生成时间"].join(" · ");
}

function safeExportFileName(name) {
  const base = String(name || "未命名论文").replace(/\.[^.]+$/, "").replace(/[\\/:*?"<>|]/g, "_").trim();
  return base || "未命名论文";
}

function exportSingleMaterialReport(paper) {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const ts = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
  const fileTs = ts.replace(/[: ]/g, "-");
  const labelOf = (m) => matModelLabels[m] || (m ? m : "");
  const stripHead = (md, name) => {
    if (!md) return "";
    const esc = String(name).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return md.replace(new RegExp("^###\\s+" + esc + "\\s*\\n+"), "");
  };
  const modelLabel = labelOf(paper.model) || paper.model_label || "";
  const contentOf = (part, fallback, strip) => {
    let content = paper[part];
    if (strip) content = stripHead(content, paper.name);
    return content && content.trim() ? content : fallback;
  };
  const md =
    "# 材料信息分析报告\n\n" +
    `论文：${paper.name || "（未命名论文）"}` + (modelLabel ? `　|　模型：${modelLabel}` : "") + `　|　导出时间：${ts}\n\n` +
    "## 一、摘要与结论详细总结\n\n" +
    contentOf("part1", "（无内容）", true) +
    "\n\n## 二、材料成分配比与性能参数对比\n\n" +
    contentOf("part2", "（未提取到材料信息）", false) +
    "\n\n## 三、综合结论与建议\n\n" +
    contentOf("part3", "（未生成结论）", true) +
    "\n";
  const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${safeExportFileName(paper.name)}_分析报告_${fileTs}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function closeMaterialExportDialog() {
  if (matExportDialog.open) matExportDialog.close();
}

function openMaterialExportDialog() {
  if (!matResults.size) {
    alert("暂无可导出的报告，请先点击「提取」。");
    return;
  }
  matExportReportList.innerHTML = "";
  matExportConfirmBtn.disabled = true;
  delete matExportConfirmBtn.dataset.reportId;
  [...matResults.entries()].forEach(([id, paper], index) => {
    const option = document.createElement("label");
    option.className = "export-report-option";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "material-export-report";
    input.value = id;
    input.addEventListener("change", () => {
      matExportConfirmBtn.disabled = false;
      matExportConfirmBtn.dataset.reportId = input.value;
    });
    const copy = document.createElement("span");
    copy.className = "export-report-option-copy";
    const title = document.createElement("span");
    title.className = "export-report-option-title";
    title.textContent = `${paper.name || "未命名论文"} + 分析报告`;
    title.title = title.textContent;
    const meta = document.createElement("span");
    meta.className = "export-report-option-meta";
    meta.textContent = reportExportMeta(paper);
    copy.appendChild(title);
    copy.appendChild(meta);
    option.appendChild(input);
    option.appendChild(copy);
    matExportReportList.appendChild(option);
  });
  matExportDialog.showModal();
  matExportReportList.querySelector('input[type="radio"]')?.focus();
}

// —— 导出：先选择一份分析报告，再确认下载 markdown ——
matExportBtn.addEventListener("click", openMaterialExportDialog);
matExportCancelBtn.addEventListener("click", closeMaterialExportDialog);
matExportCancelBottomBtn.addEventListener("click", closeMaterialExportDialog);
matExportDialog.addEventListener("click", (event) => {
  if (event.target === matExportDialog) closeMaterialExportDialog();
});
matExportConfirmBtn.addEventListener("click", () => {
  const reportId = matExportConfirmBtn.dataset.reportId;
  const paper = reportId && matResults.get(reportId);
  if (!paper) return;
  exportSingleMaterialReport(paper);
  closeMaterialExportDialog();
});

// —— 第一栏底部「＋」：添加本地 PDF，转换后直接进「论文列表」（不落任何库）——
matPlusBtn.addEventListener("click", () => matFileInput.click());
matFileInput.addEventListener("change", () => {
  if (!matFileInput.files.length) return;
  for (const f of matFileInput.files) {
    if (!f.name.toLowerCase().endsWith(".pdf")) {
      alert("不支持的文件类型：" + f.name + "（仅支持 .pdf）");
      continue;
    }
    if (convertedFiles.some((p) => p.name === f.name)) {
      alert("论文列表中已存在同名文件：" + f.name);
      continue;
    }

    // 不等待上一个 PDF 完成：函数会同步创建「准备中」条目，后续预处理各自后台进行。
    void preparePaperForMaterial(f.name, "", f, { origin: "local", select: true }).catch((e) => {
      alert("PDF 预处理失败（" + f.name + "）：" + e.message);
    });
  }
  matFileInput.value = "";
});

// 进入页面即拉取后端可用模型，填充「材料信息提取」的模型选择下拉框
loadMatModels();
void loadKnowledgeState(false);

// 初始：进入页面即无共享数据（论文库已移除，刷新即全新状态）



/* ==================== V2 材料工作区桥接 ==================== */
window.materialWorkspaceBridge = {
  getFiles() {
    return convertedFiles;
  },
  getModelLabels() {
    return { ...matModelLabels };
  },
  preparePaperForMaterial,
  async removeFile(documentId) {
    const index = convertedFiles.findIndex((item) => item.id === documentId);
    if (index < 0) return false;
    const paper = convertedFiles[index];
    paper.prepareRunId = (paper.prepareRunId || 0) + 1;
    if (paper.prepareTaskId) {
      await fetch("/api/stop_paper_prepare/" + paper.prepareTaskId, { method: "POST" }).catch(() => {});
      paper.prepareTaskId = "";
    }
    convertedFiles.splice(index, 1);
    matSelected.delete(documentId);
    if (typeof chatAttachPapers !== "undefined") chatAttachPapers.delete(documentId);
    renderMatFiles();
    renderAttachPanel();
    renderChips();
    return true;
  },
  renderMarkdown(container, markdown) {
    renderMarkdown(container, markdown || "");
  },
  figureMapForDocument(documentId) {
    const source = convertedFiles.find((paper) => paper.id === documentId);
    return figureMapFromIndex(source && source.figureIndex);
  },
  createFigurePreviewPanel,
  createMultiFigurePreviewPanel,
  renderMarkdownWithFigureReferences(container, markdown, figureMap, figurePreview) {
    const togglePreview = typeof figurePreview === "function"
      ? figurePreview
      : figurePreview && (figurePreview.show || figurePreview.toggle);
    const isPreviewSelected = figurePreview && typeof figurePreview.isSelected === "function"
      ? figurePreview.isSelected
      : null;
    const registerReference = figurePreview && typeof figurePreview.registerReference === "function"
      ? figurePreview.registerReference
      : null;
    renderReportMarkdown(container, markdown || "", figureMap || {}, togglePreview, isPreviewSelected, registerReference);
  },
  openArchive(reportId, paper) {
    openKnowledgeArchiveDialog(reportId, paper);
  },
  isArchived(reportId) {
    const report = archivedReportForSource(reportId);
    return Boolean(report && foldersForReport(report.id).length > 0);
  },
};



