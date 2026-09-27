import type { Citation } from './types';

function getKnowledgeAssetContentUrl(kbId: string, assetId: string): string {
  // 必须与后端 `apps/knowledge-service/src/mcp/server.ts` 的代理地址一致：
  // 走相对路径让浏览器自动带同源 cookie。
  return `/api/knowledge-bases/${kbId}/assets/${assetId}/content`;
}

export { getKnowledgeAssetContentUrl };

export function CitationList({
  citations,
  onPreviewImage,
}: {
  citations: Citation[];
  onPreviewImage: (url: string, filename?: string) => void;
}) {
  if (!citations.length) return null;
  const imageCount = citations.reduce((sum, c) => sum + (c.images?.length ?? 0), 0);
  return (
    <section className="citation-list" aria-label="引用来源">
      <div className="citation-list-header">
        <h4>引用来源</h4>
        {imageCount > 0 ? <span className="citation-list-badge">含 {imageCount} 张图</span> : null}
      </div>
      <ul>
        {citations.map((citation) => (
          <li key={citation.chunkId}>
            <div className="citation-row">
              <strong>{citation.documentName}</strong>
              <span>#{citation.ordinal}{citation.heading ? ` · ${citation.heading}` : ''}</span>
              <span>score {citation.score.toFixed(2)} · via {citation.via}</span>
            </div>
            {citation.images && citation.images.length > 0 ? (
              <div className="citation-images">
                {citation.images.map((image) => {
                  const url = getKnowledgeAssetContentUrl(citation.kbId, image.assetId);
                  const label = image.alt || image.name;
                  return (
                    <button
                      type="button"
                      key={image.assetId}
                      className="citation-image"
                      onClick={() => onPreviewImage(url, label)}
                      aria-label={`查看图片 ${label}`}
                      title={label}
                    >
                      <img src={url} alt={label} loading="lazy" />
                    </button>
                  );
                })}
              </div>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
