export type NavigationDecision = 'allow' | 'external' | 'deny';

const EXTERNAL_PROTOCOLS = new Set(['http:', 'https:']);

export function classifyNavigation(target: URL, appOrigin: string): NavigationDecision {
  if (!EXTERNAL_PROTOCOLS.has(target.protocol)) {
    return 'deny';
  }

  const configuredOrigin = new URL(appOrigin).origin;
  return target.origin === configuredOrigin ? 'allow' : 'external';
}
