/**
 * 跨上下文剪贴板写入工具
 *
 * navigator.clipboard 仅在 HTTPS 或 localhost 环境下可用。
 * 通过 IP 地址访问的 HTTP 页面（非 localhost）中，navigator.clipboard 为
 * undefined，导致复制静默失败。
 *
 * 该工具先尝试现代 API，失败时降级到 execCommand('copy') 的兼容实现。
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  // 现代路径：Clipboard API（HTTPS / localhost）
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // 权限被拒绝或其他错误时继续降级
    }
  }

  // 降级路径：execCommand（适用于 HTTP + IP 地址等非安全上下文）
  try {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    // 避免页面跳动
    textarea.style.position = 'fixed';
    textarea.style.top = '0';
    textarea.style.left = '0';
    textarea.style.width = '1px';
    textarea.style.height = '1px';
    textarea.style.padding = '0';
    textarea.style.border = 'none';
    textarea.style.outline = 'none';
    textarea.style.boxShadow = 'none';
    textarea.style.background = 'transparent';
    textarea.style.opacity = '0';
    textarea.style.pointerEvents = 'none';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(textarea);
    return ok;
  } catch {
    return false;
  }
}
