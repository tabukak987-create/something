(function (global) {
  if (global.marked && typeof global.marked.parse === 'function') {
    return;
  }

  function escapeHtml(text) {
    return String(text || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderInline(text) {
    let html = escapeHtml(text);
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return html;
  }

  function renderParagraph(lines, output) {
    const text = lines.join(' ').trim();
    if (text) {
      output.push(`<p>${renderInline(text)}</p>`);
    }
  }

  function renderMarkdown(markdown) {
    const lines = String(markdown || '').replace(/\r\n?/g, '\n').split('\n');
    const output = [];
    let paragraph = [];
    let codeFence = null;
    let codeLines = [];
    let listType = null;
    let listItems = [];
    let blockquote = [];

    function flushParagraph() {
      if (paragraph.length) {
        renderParagraph(paragraph, output);
        paragraph = [];
      }
    }

    function flushList() {
      if (!listType || !listItems.length) return;
      const tag = listType;
      output.push(`<${tag}>${listItems.map((item) => `<li>${renderInline(item)}</li>`).join('')}</${tag}>`);
      listType = null;
      listItems = [];
    }

    function flushBlockquote() {
      if (!blockquote.length) return;
      output.push(`<blockquote>${blockquote.map((item) => `<p>${renderInline(item)}</p>`).join('')}</blockquote>`);
      blockquote = [];
    }

    for (const rawLine of lines) {
      const line = rawLine.replace(/\s+$/g, '');
      const trimmed = line.trim();

      if (codeFence !== null) {
        if (trimmed.startsWith('```')) {
          output.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
          codeFence = null;
          codeLines = [];
        } else {
          codeLines.push(line);
        }
        continue;
      }

      if (!trimmed) {
        flushParagraph();
        flushList();
        flushBlockquote();
        continue;
      }

      if (trimmed.startsWith('```')) {
        flushParagraph();
        flushList();
        flushBlockquote();
        codeFence = trimmed.slice(3).trim() || 'text';
        codeLines = [];
        continue;
      }

      const headingMatch = /^(#{1,6})\s+(.+)$/.exec(trimmed);
      if (headingMatch) {
        flushParagraph();
        flushList();
        flushBlockquote();
        const level = headingMatch[1].length;
        output.push(`<h${level}>${renderInline(headingMatch[2])}</h${level}>`);
        continue;
      }

      if (/^>\s?/.test(trimmed)) {
        flushParagraph();
        flushList();
        blockquote.push(trimmed.replace(/^>\s?/, ''));
        continue;
      }

      const orderedMatch = /^\d+\.\s+(.+)$/.exec(trimmed);
      if (orderedMatch) {
        flushParagraph();
        flushBlockquote();
        if (listType && listType !== 'ol') flushList();
        listType = 'ol';
        listItems.push(orderedMatch[1]);
        continue;
      }

      const bulletMatch = /^[-*+]\s+(.+)$/.exec(trimmed);
      if (bulletMatch) {
        flushParagraph();
        flushBlockquote();
        if (listType && listType !== 'ul') flushList();
        listType = 'ul';
        listItems.push(bulletMatch[1]);
        continue;
      }

      flushList();
      flushBlockquote();
      paragraph.push(line);
    }

    flushParagraph();
    flushList();
    flushBlockquote();

    return output.join('\n');
  }

  global.SimpleMarkdown = { parse: renderMarkdown };
  global.marked = { parse: renderMarkdown };
})(window);