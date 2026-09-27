import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { createPortal } from 'react-dom';

import { scheduleMicrotask } from '@/app/lib/schedule-microtask';

import { apiFetch } from './api';
import { Icon } from './icon';

type LightboxImage = {
  /** 普通位图（聊天图片/附件）的地址。 */
  url?: string;
  /** Mermaid 渲染出的 SVG 字符串，与 url 二选一。 */
  svg?: string;
  filename?: string;
};

const MIN_SCALE = 1;
const MAX_SCALE = 4;
const SCALE_STEP = 0.25;
/**
 * 下载用临时 blob URL 的释放延时。仅用于回收前端内存：
 * 字节此时已通过 apiFetch 完整下载，不参与任何任务/请求/重试/成败逻辑；
 * 延后释放只是兼容个别浏览器在 anchor.click() 后异步取 blob，
 * 即使该定时器不执行，最坏也只是内存留到页面关闭，无任何业务副作用。
 */
const DOWNLOAD_BLOB_RELEASE_DELAY_MS = 30_000;

/**
 * 聊天图片大图查看：Portal 挂到 body（避开 transform 容器造成的定位/缩放问题），
 * 支持滚轮/按钮缩放（1x–4x）与按住拖动平移；Esc 或点击遮罩关闭。
 */
function Lightbox({
  image,
  onClose,
}: {
  image: LightboxImage | null;
  onClose: () => void;
}) {
  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  // Mermaid SVG 只有 viewBox + width:100%，放进收缩包裹容器会解析成 0 尺寸；
  // 因此按 viewBox 宽高比与视口上限算出确定的卡片像素尺寸。
  const [diagramSize, setDiagramSize] = useState<{ width: number; height: number } | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const dragRef = useRef<{ startX: number; startY: number; baseX: number; baseY: number } | null>(
    null,
  );
  const overlayRef = useRef<HTMLDivElement | null>(null);

  const reset = useCallback(() => {
    setScale(1);
    setOffset({ x: 0, y: 0 });
  }, []);

  // 切换图片时复位；Esc 关闭并锁定背景滚动；wheel 走原生 listener 可显式 passive: false。
  useEffect(() => {
    if (!image) return;
    scheduleMicrotask(reset);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    const overlay = overlayRef.current;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      setScale((current) => {
        const delta = event.deltaY < 0 ? SCALE_STEP : -SCALE_STEP;
        const next = Number((current + delta).toFixed(2));
        const clamped = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next));
        if (clamped === MIN_SCALE) setOffset({ x: 0, y: 0 });
        return clamped;
      });
    };
    overlay?.addEventListener('wheel', onWheel, { passive: false });

    return () => {
      window.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousOverflow;
      overlay?.removeEventListener('wheel', onWheel);
    };
  }, [image, onClose, reset]);

  // SVG 浮层：注入后读 viewBox 计算与内容等比的白底卡片尺寸；窗口缩放时重算。
  useEffect(() => {
    if (!image?.svg) {
      scheduleMicrotask(() => setDiagramSize(null));
      return;
    }
    const CARD_PADDING = 22;
    const compute = () => {
      const svg = document.querySelector<SVGSVGElement>('.lightbox-svg svg');
      const viewBox = svg?.viewBox?.baseVal;
      if (!viewBox || viewBox.width <= 0 || viewBox.height <= 0) return;
      const ratio = viewBox.width / viewBox.height;
      const maxWidth = window.innerWidth * 0.88;
      const maxHeight = window.innerHeight * 0.82;
      let contentWidth = maxWidth - CARD_PADDING * 2;
      let contentHeight = contentWidth / ratio;
      if (contentHeight > maxHeight - CARD_PADDING * 2) {
        contentHeight = maxHeight - CARD_PADDING * 2;
        contentWidth = contentHeight * ratio;
      }
      setDiagramSize({
        width: Math.round(contentWidth + CARD_PADDING * 2),
        height: Math.round(contentHeight + CARD_PADDING * 2),
      });
    };
    // SVG 与本次提交一同注入，下一帧即可读到 viewBox。
    const raf = requestAnimationFrame(compute);
    window.addEventListener('resize', compute);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', compute);
    };
  }, [image]);

  if (!image) return null;

  const zoomBy = (delta: number) => {
    setScale((current) => {
      const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, Number((current + delta).toFixed(2))));
      if (next === MIN_SCALE) setOffset({ x: 0, y: 0 });
      return next;
    });
  };

  // 图片与 SVG 图表共用同一套缩放/拖动交互。
  const mediaStyle: CSSProperties = {
    transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
    cursor: scale > 1 ? (isDragging ? 'grabbing' : 'grab') : 'default',
  };
  const mediaHandlers = {
    onClick: (event: ReactMouseEvent) => event.stopPropagation(),
    onPointerDown: (event: ReactPointerEvent<HTMLElement>) => {
      if (scale <= 1) return;
      event.currentTarget.setPointerCapture(event.pointerId);
      dragRef.current = {
        startX: event.clientX,
        startY: event.clientY,
        baseX: offset.x,
        baseY: offset.y,
      };
      setIsDragging(true);
    },
    onPointerMove: (event: ReactPointerEvent<HTMLElement>) => {
      const drag = dragRef.current;
      if (!drag) return;
      setOffset({
        x: drag.baseX + (event.clientX - drag.startX),
        y: drag.baseY + (event.clientY - drag.startY),
      });
    },
    onPointerUp: (event: ReactPointerEvent<HTMLElement>) => {
      dragRef.current = null;
      setIsDragging(false);
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    },
    onDoubleClick: () => {
      if (scale > 1) reset();
      else setScale(2);
    },
  };

  const kindLabel = image.svg ? '图表' : '图片';

  return createPortal(
    <div
      ref={overlayRef}
      className="lightbox-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={image.filename ? `${kindLabel}预览：${image.filename}` : `${kindLabel}预览`}
      onClick={onClose}
    >
      <div className="lightbox-toolbar" onClick={(event) => event.stopPropagation()}>
        <span className="lightbox-filename" title={image.filename}>
          {image.filename}
        </span>
        <div className="lightbox-actions">
          <button
            type="button"
            aria-label="缩小"
            disabled={scale <= MIN_SCALE}
            onClick={() => zoomBy(-SCALE_STEP)}
          >
            <Icon name="zoom-out" size={17} />
          </button>
          <span className="lightbox-scale">{Math.round(scale * 100)}%</span>
          <button
            type="button"
            aria-label="放大"
            disabled={scale >= MAX_SCALE}
            onClick={() => zoomBy(SCALE_STEP)}
          >
            <Icon name="zoom-in" size={17} />
          </button>
          <button type="button" aria-label="复位" onClick={reset}>
            <Icon name="refresh" size={15} />
          </button>
          <button type="button" aria-label="关闭" onClick={onClose}>
            <Icon name="x" size={17} />
          </button>
        </div>
      </div>
      {image.svg ? (
        <div
          className="lightbox-image lightbox-svg"
          // 图表 SVG 由 mermaid 本地渲染生成（非用户可控 HTML），安全注入。
          dangerouslySetInnerHTML={{ __html: image.svg }}
          style={{
            ...mediaStyle,
            width: diagramSize?.width,
            height: diagramSize?.height,
          }}
          {...mediaHandlers}
        />
      ) : (
        <img
          alt={image.filename ?? '聊天图片大图'}
          className="lightbox-image"
          draggable={false}
          src={image.url}
          style={mediaStyle}
          {...mediaHandlers}
        />
      )}
    </div>,
    document.body,
  );
}

/**
 * 下载 attachment content URL：fetch 带 cookie 跟进 302 → 拿 blob，
 * 用临时 <a download> 触发浏览器下载，然后释放 object URL。
 * chrome 下载管理器不续 cookie，直接 `<a href>` 会 401。
 */
async function triggerAttachmentDownload(url: string, filename?: string): Promise<void> {
  const response = await apiFetch(url);
  if (!response.ok) {
    throw new Error(`下载失败 HTTP ${response.status}`);
  }
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  if (filename) anchor.download = filename;
  anchor.rel = 'noopener';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(objectUrl), DOWNLOAD_BLOB_RELEASE_DELAY_MS);
}

export { Lightbox, triggerAttachmentDownload, type LightboxImage };
