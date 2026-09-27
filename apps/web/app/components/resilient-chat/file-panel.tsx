import { useEffect, useMemo, useRef, useState } from 'react';
import hljs from 'highlight.js';

import { scheduleMicrotask } from '@/app/lib/schedule-microtask';

import { Icon } from './icon';

type TouchedFile = {
  path: string;
  /** 最近一次可展示的内容来源：write_file/edit_file 的 input.content，或 read_file 的 output。 */
  content: string | null;
  /** 最后一次操作类型，用于区分新建/修改/读取。 */
  operation: 'write_file' | 'edit_file' | 'read_file' | 'delete';
};

type FileNode = {
  name: string;
  /** 相对当前节点的路径（如 src/components/foo.tsx）。 */
  path: string;
  isDir: boolean;
  children: FileNode[];
  file?: TouchedFile;
};

function buildFileTree(files: TouchedFile[]): FileNode[] {
  const root: FileNode = { name: '', path: '', isDir: true, children: [] };
  for (const file of files) {
    const segments = file.path.split('/').filter(Boolean);
    let current = root;
    let acc = '';
    for (let i = 0; i < segments.length; i++) {
      const segment = segments[i];
      acc = acc ? `${acc}/${segment}` : segment;
      const isLast = i === segments.length - 1;
      let node = current.children.find((child) => child.name === segment);
      if (!node) {
        node = {
          name: segment,
          path: acc,
          isDir: !isLast,
          children: [],
          file: isLast ? file : undefined,
        };
        current.children.push(node);
      } else if (isLast) {
        node.file = file;
      }
      current = node;
    }
  }
  return root.children;
}

function extOf(name: string) {
  const idx = name.lastIndexOf('.');
  return idx >= 0 ? name.slice(idx + 1).toLowerCase() : '';
}

function languageFromPath(path: string) {
  const ext = extOf(path);
  const map: Record<string, string> = {
    ts: 'typescript',
    tsx: 'tsx',
    js: 'javascript',
    jsx: 'jsx',
    json: 'json',
    css: 'css',
    html: 'html',
    md: 'markdown',
    py: 'python',
    sh: 'bash',
    yml: 'yaml',
    yaml: 'yaml',
  };
  return map[ext] ?? 'plaintext';
}

function FileTree({
  nodes,
  selectedPath,
  onSelect,
}: {
  nodes: FileNode[];
  selectedPath: string | null;
  onSelect: (file: TouchedFile) => void;
}) {
  return (
    <ul className="file-tree">
      {nodes.map((node) => (
        <FileTreeNode
          key={node.path}
          node={node}
          depth={0}
          selectedPath={selectedPath}
          onSelect={onSelect}
        />
      ))}
    </ul>
  );
}

function FileTreeNode({
  depth,
  node,
  onSelect,
  selectedPath,
}: {
  depth: number;
  node: FileNode;
  onSelect: (file: TouchedFile) => void;
  selectedPath: string | null;
}) {
  const [open, setOpen] = useState(true);
  if (node.isDir) {
    return (
      <li>
        <button
          aria-expanded={open}
          className="file-dir"
          style={{ paddingLeft: 10 + depth * 14 }}
          type="button"
          onClick={() => setOpen((current) => !current)}
        >
          <Icon name="chevron" size={14} />
          <Icon name="folder" size={15} />
          <span>{node.name}</span>
        </button>
        {open && (
          <ul>
            {node.children.map((child) => (
              <FileTreeNode
                key={child.path}
                node={child}
                depth={depth + 1}
                selectedPath={selectedPath}
                onSelect={onSelect}
              />
            ))}
          </ul>
        )}
      </li>
    );
  }
  const selected = node.file?.path === selectedPath;
  return (
    <li>
      <button
        className={`file-item ${selected ? 'is-selected' : ''}`}
        style={{ paddingLeft: 10 + depth * 14 + 18 }}
        type="button"
        onClick={() => node.file && onSelect(node.file)}
      >
        <Icon name="file" size={14} />
        <span className="file-item-name">{node.name}</span>
        <span className={`file-op file-op-${node.file?.operation ?? 'read_file'}`}>
          {node.file?.operation === 'write_file'
            ? '新建'
            : node.file?.operation === 'edit_file'
              ? '修改'
              : node.file?.operation === 'delete'
                ? '删除'
                : '读取'}
        </span>
      </button>
    </li>
  );
}

/** 判断文件是否可在浏览器内直接运行（HTML/SVG 等）。 */
function isBrowserRunnable(path: string): boolean {
  const ext = extOf(path);
  return ext === 'html' || ext === 'htm' || ext === 'svg';
}

/** 用 Blob 触发浏览器下载：文件内容已在前端内存中，无需走后端。 */
function downloadTouchedFile(file: TouchedFile) {
  if (file.content === null) return;
  const ext = extOf(file.path);
  const mimeMap: Record<string, string> = {
    html: 'text/html',
    htm: 'text/html',
    svg: 'image/svg+xml',
    css: 'text/css',
    js: 'text/javascript',
    json: 'application/json',
    md: 'text/markdown',
    txt: 'text/plain',
  };
  const blob = new Blob([file.content], {
    type: mimeMap[ext] ?? 'application/octet-stream',
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = file.path.split('/').pop() ?? 'file';
  anchor.rel = 'noopener';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4_000);
}

function FilePreview({ file, previewUrl, isBuildPreview }: { file: TouchedFile; previewUrl?: string; isBuildPreview?: boolean }) {
  const lang = languageFromPath(file.path);
  const runnable = isBrowserRunnable(file.path);
  // 默认对可运行文件展示浏览器预览，其余展示代码；用户可切换。
  const [viewMode, setViewMode] = useState<'code' | 'browser'>(
    runnable ? 'browser' : 'code',
  );
  // 用 highlight.js 对代码内容做语法高亮，返回带 hljs class 的 HTML 字符串。
  const highlighted = useMemo(() => {
    if (file.content === null) return null;
    try {
      const result =
        lang !== 'plaintext' && hljs.getLanguage(lang)
          ? hljs.highlight(file.content, { language: lang, ignoreIllegals: true })
          : hljs.highlightAuto(file.content);
      return result.value;
    } catch {
      return null;
    }
  }, [file.content, lang]);

  if (file.content === null && !isBuildPreview) {
    return (
      <div className="file-preview-empty">
        <Icon name="folder" size={28} />
        <p>{file.path}</p>
        <small>该文件没有可预览的内容（仅记录了路径操作）。</small>
      </div>
    );
  }
  return (
    <div className="file-preview">
      <div className="file-preview-head">
        <span className="file-preview-path">{file.path}</span>
        <div className="file-preview-actions">
          {runnable && (
            <div className="file-view-switch" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={viewMode === 'browser'}
                className={viewMode === 'browser' ? 'is-active' : ''}
                onClick={() => setViewMode('browser')}
                title="在浏览器中运行页面"
              >
                浏览器
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={viewMode === 'code'}
                className={viewMode === 'code' ? 'is-active' : ''}
                onClick={() => setViewMode('code')}
                title="查看源代码"
              >
                代码
              </button>
            </div>
          )}
          {!runnable && <span className="file-preview-lang">{lang}</span>}
          <button
            type="button"
            className="file-download-btn"
            title="下载该文件"
            onClick={() => downloadTouchedFile(file)}
          >
            <Icon name="download" size={14} />
          </button>
        </div>
      </div>
      {isBuildPreview && previewUrl ? (
        <iframe
          className="file-browser-frame"
          title="构建预览"
          src={previewUrl}
        />
      ) : viewMode === 'browser' && runnable ? (
        <iframe
          className="file-browser-frame"
          title={`预览 ${file.path}`}
          sandbox="allow-scripts allow-same-origin"
          srcDoc={file.content ?? ''}
        />
      ) : previewUrl ? (
        <iframe
          className="file-browser-frame"
          title="实时预览"
          src={previewUrl}
        />
      ) : (
        <pre className={`file-preview-code hljs language-${lang}`}>
          {highlighted ? (
            <code dangerouslySetInnerHTML={{ __html: highlighted }} />
          ) : (
            <code>{file.content}</code>
          )}
        </pre>
      )}
    </div>
  );
}

const MIN_FILES_WIDTH = 320;
const DEFAULT_FILES_WIDTH = 420;
/** 侧边栏原始宽度（用于计算文件面板最大可拖到的位置）。 */
const SIDEBAR_WIDTH = 264;

/** 文件面板左边缘的拖拽条：左右拖动改变整个文件面板的宽度。
 *  拖到最小宽度后继续向右拖超过阈值，则关闭文件面板。
 *  支持拖动超过屏幕中心，此时会触发左侧导航隐藏，并允许继续拖到接近左边缘。 */
function PanelResizer({
  onResize,
  onClose,
  onDoubleClick,
  onHideSidebar,
}: {
  onResize: (width: number) => void;
  onClose: () => void;
  onDoubleClick: () => void;
  /** 当文件面板宽度超过屏幕一半时调用，隐藏左侧导航。 */
  onHideSidebar?: () => void;
}) {
  const dragRef = useRef<{ x: number; width: number; max: number } | null>(null);
  const closedRef = useRef(false);
  const sidebarHiddenRef = useRef(false);
  // 用 ref 保存最新回调，避免闭包过期
  const onResizeRef = useRef(onResize);
  const onCloseRef = useRef(onClose);
  const onHideSidebarRef = useRef(onHideSidebar);
  useEffect(() => {
    onResizeRef.current = onResize;
    onCloseRef.current = onClose;
    onHideSidebarRef.current = onHideSidebar;
  }, [onResize, onClose, onHideSidebar]);

  function handlePointerDown(event: React.PointerEvent<HTMLDivElement>) {
    event.preventDefault();
    event.stopPropagation();
    closedRef.current = false;
    sidebarHiddenRef.current = false;
    const panel = event.currentTarget.closest('.file-panel') as HTMLElement | null;
    const currentWidth = panel?.offsetWidth ?? DEFAULT_FILES_WIDTH;
    dragRef.current = {
      x: event.clientX,
      width: currentWidth,
      max: window.innerWidth - SIDEBAR_WIDTH,
    };
    document.body.classList.add('is-file-resizing');
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      /* 忽略 */
    }
  }

  function handlePointerMove(event: React.PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag) return;
    const delta = drag.x - event.clientX;
    const next = Math.min(
      drag.max,
      Math.max(MIN_FILES_WIDTH, drag.width + delta),
    );
    onResizeRef.current(next);

    if (!sidebarHiddenRef.current && onHideSidebarRef.current) {
      if (next > window.innerWidth / 2) {
        sidebarHiddenRef.current = true;
        onHideSidebarRef.current();
      }
    }

    if (
      !closedRef.current &&
      next === MIN_FILES_WIDTH &&
      delta < drag.width - MIN_FILES_WIDTH - 40
    ) {
      closedRef.current = true;
      onCloseRef.current();
    }
  }

  function handlePointerUp(event: React.PointerEvent<HTMLDivElement>) {
    dragRef.current = null;
    document.body.classList.remove('is-file-resizing');
    try {
      event.currentTarget.releasePointerCapture(event.pointerId);
    } catch {
      /* 忽略 */
    }
  }

  return (
    <div
      aria-label="拖动调整文件面板宽度"
      aria-orientation="vertical"
      className="file-panel-resizer"
      role="separator"
      onDoubleClick={onDoubleClick}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
    />
  );
}

/** 文件树与预览区之间的拖拽条：左右拖动改变目录列宽度。
 *  当目录宽度超过文件面板一半时，完全隐藏左侧导航以腾出空间。 */
function TreeResizer({
  onResize,
  onHideSidebar,
}: {
  onResize: (width: number) => void;
  onHideSidebar: () => void;
}) {
  const dragRef = useRef<{ x: number; width: number } | null>(null);
  const hiddenRef = useRef(false);
  const onResizeRef = useRef(onResize);
  const onHideSidebarRef = useRef(onHideSidebar);
  useEffect(() => {
    onResizeRef.current = onResize;
    onHideSidebarRef.current = onHideSidebar;
  }, [onResize, onHideSidebar]);

  function handlePointerDown(event: React.PointerEvent<HTMLDivElement>) {
    event.preventDefault();
    event.stopPropagation();
    hiddenRef.current = false;
    const body = event.currentTarget.closest('.file-panel-body') as HTMLElement | null;
    const treePane = body?.querySelector('.file-tree-pane') as HTMLElement | null;
    dragRef.current = { x: event.clientX, width: treePane?.offsetWidth ?? 200 };
    document.body.classList.add('is-file-resizing');
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      /* 忽略 */
    }
  }

  function handlePointerMove(event: React.PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag) return;
    const nextWidth = drag.width + event.clientX - drag.x;
    onResizeRef.current(nextWidth);
    if (hiddenRef.current) return;
    const panel = document.querySelector('.file-panel') as HTMLElement | null;
    const panelWidth = panel?.offsetWidth ?? 420;
    const maxWidth = panelWidth - 80;
    const clamped = Math.max(120, Math.min(maxWidth, nextWidth));
    if (clamped > panelWidth / 2) {
      hiddenRef.current = true;
      onHideSidebarRef.current();
    }
  }

  function handlePointerUp(event: React.PointerEvent<HTMLDivElement>) {
    dragRef.current = null;
    document.body.classList.remove('is-file-resizing');
    try {
      event.currentTarget.releasePointerCapture(event.pointerId);
    } catch {
      /* 忽略 */
    }
  }

  return (
    <div
      aria-label="拖动调整目录宽度"
      aria-orientation="vertical"
      className="file-tree-resizer"
      role="separator"
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
    />
  );
}

function FilePanel({
  files,
  onClose,
  onResize,
  onResetWidth,
  onExpand,
  isWide,
  onToggleWide,
  selectedPath,
  onSelectPath,
  onHideSidebar,
  sessionId,
  openBuildPreview,
}: {
  files: TouchedFile[];
  onClose: () => void;
  onResize: (width: number) => void;
  onResetWidth: () => void;
  /** 展开文件面板至宽屏模式（70% 视口）。 */
  onExpand: () => void;
  /** 当前是否为宽屏展开状态（由父组件控制）。 */
  isWide: boolean;
  /** 切换宽屏/默认宽度。 */
  onToggleWide: () => void;
  /** 外部受控选中路径：从聊天消息的文件链接跳转时由父组件设置。 */
  selectedPath: string | null;
  onSelectPath: (path: string) => void;
  /** 目录拖宽超过文件面板一半时完全隐藏左侧导航。 */
  onHideSidebar: () => void;
  /** 当前会话 ID，用于加载构建预览。 */
  sessionId?: string | null;
  /** 外部触发打开构建预览的计数器，每次 +1 触发 useEffect（从聊天消息的预览按钮点击时由父组件递增）。 */
  openBuildPreview?: number;
}) {
  const tree = useMemo(() => buildFileTree(files), [files]);
  const selected = useMemo(
    () => files.find((file) => file.path === selectedPath) ?? files[0] ?? null,
    [files, selectedPath],
  );
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [treeWidth, setTreeWidth] = useState(200);
  const [treeVisible, setTreeVisible] = useState(true);
  const [buildPreview, setBuildPreview] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [previewRefreshKey, setPreviewRefreshKey] = useState(0);
  const previewUrl = sessionId && buildPreview
    ? `/api/agent/sessions/${sessionId}/preview/?_=${previewRefreshKey}`
    : '';
  console.log('[FilePanel] preview state:', { sessionId, buildPreview, previewUrl });

  // 切换会话时 files 变化，重置树宽度避免布局错乱
  useEffect(() => {
    scheduleMicrotask(() => setTreeWidth(200));
  }, [files]);

  // 外部触发打开构建预览（用户点击聊天消息中的预览按钮）
  useEffect(() => {
    console.log('[FilePanel] openBuildPreview changed:', { openBuildPreview, buildPreview });
    if (openBuildPreview && openBuildPreview > 0) {
      scheduleMicrotask(() => {
        setBuildPreview(true);
        setPreviewRefreshKey((k) => k + 1);
      });
    }
  }, [openBuildPreview]);

  function handleTreeResize(next: number) {
    const panel = document.querySelector('.file-panel') as HTMLElement | null;
    const max = (panel?.offsetWidth ?? 420) - 80;
    setTreeWidth(Math.min(max, Math.max(120, next)));
  }

  return (
    <aside
      className={`file-panel ${isFullscreen ? 'is-fullscreen' : ''}`}
      aria-label="AI 生成的文件"
    >
      {!isFullscreen && (
        <PanelResizer onResize={onResize} onClose={onClose} onDoubleClick={onResetWidth} onHideSidebar={onHideSidebar} />
      )}
      <header className="file-panel-head">
        <div className="file-panel-title">
          <Icon name="folder" size={17} />
          <strong>文件</strong>
          <span className="file-panel-count">{files.length}</span>
        </div>
        <div className="file-panel-actions">
          <button
            aria-label={isWide ? '收起文件面板' : '展开文件面板'}
            className={`icon-button expand-toggle${isWide ? ' is-active' : ''}`}
            data-tooltip={isWide ? '收起面板' : '展开面板'}
            type="button"
            onClick={onToggleWide}
          >
            <Icon name="chevron" size={15} />
          </button>
          {sessionId && (
            <button
              aria-label="构建预览"
              className={`icon-button${buildPreview ? ' is-active' : ''}`}
              data-tooltip={buildPreview ? '关闭预览' : '开启预览'}
              type="button"
              onClick={() => setBuildPreview((v) => !v)}
            >
              <Icon name="home" size={15} />
            </button>
          )}
          {buildPreview && sessionId && (
            <button
              aria-label="重新构建"
              className={`icon-button${rebuilding ? ' is-active' : ''}`}
              data-tooltip="刷新预览"
              type="button"
              disabled={rebuilding}
              onClick={async () => {
                setRebuilding(true);
                try {
                  const resp = await fetch(`/api/agent/sessions/${sessionId}/rebuild`, { method: 'POST' });
                  if (resp.ok) {
                    setPreviewRefreshKey((k) => k + 1);
                  }
                } finally {
                  setRebuilding(false);
                }
              }}
            >
              <Icon name="refresh" size={15} />
            </button>
          )}
          <button
            aria-label={treeVisible ? '隐藏文件目录' : '显示文件目录'}
            className="icon-button"
            data-tooltip={treeVisible ? '隐藏文件目录' : '显示文件目录'}
            type="button"
            onClick={() => setTreeVisible((v) => !v)}
          >
            <Icon name="panel" size={15} />
          </button>
          <button
            aria-label={isFullscreen ? '退出全屏' : '全屏浏览'}
            className="icon-button"
            data-tooltip={isFullscreen ? '退出全屏' : '全屏浏览'}
            type="button"
            onClick={() => setIsFullscreen((v) => !v)}
          >
            <Icon name="maximize" size={15} />
          </button>
          <button
            aria-label="关闭文件面板"
            className="icon-button"
            data-tooltip="关闭面板"
            type="button"
            onClick={onClose}
          >
            <Icon name="x" size={16} />
          </button>
        </div>
      </header>

      {files.length === 0 ? (
        <div className="file-panel-empty">
          <Icon name="folder" size={30} />
          <p>还没有生成的文件</p>
          <small>AI 写入或修改文件后会出现在这里。</small>
        </div>
      ) : (
        <div className="file-panel-body" style={{ gridTemplateColumns: treeVisible ? `${treeWidth}px 12px minmax(0, 1fr)` : 'minmax(0, 1fr)' }}>
          {treeVisible && (
            <>
              <div className="file-tree-pane">
                <FileTree
                  nodes={tree}
                  selectedPath={selected?.path ?? null}
                  onSelect={(file) => onSelectPath(file.path)}
                />
              </div>
              <TreeResizer onResize={handleTreeResize} onHideSidebar={onHideSidebar} />
            </>
          )}
          <div className="file-preview-pane" style={!treeVisible ? { gridColumn: '1' } : undefined}>
            {buildPreview && previewUrl ? (
              <FilePreview
                file={{ path: 'dist/index.html', content: null, operation: 'read_file' }}
                previewUrl={previewUrl}
                isBuildPreview={true}
              />
            ) : selected ? (
              <FilePreview file={selected} previewUrl={previewUrl} isBuildPreview={buildPreview} />
            ) : (
              <div className="file-preview-empty">
                <Icon name="folder" size={28} />
                <p>选择左侧文件查看内容</p>
              </div>
            )}
          </div>
        </div>
      )}
    </aside>
  );
}

export {
  DEFAULT_FILES_WIDTH,
  FilePanel,
  type TouchedFile,
};
