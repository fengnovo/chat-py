'use client';

import { KnowledgeManager } from './knowledge-manager';
import { AuthGate } from '../components/auth/auth-gate';

export default function KnowledgePage() {
  return (
    <AuthGate>
      <KnowledgeManager />
    </AuthGate>
  );
}
