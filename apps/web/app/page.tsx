import { ResilientChat } from '@/app/components/resilient-chat';
import { AuthGate } from './components/auth/auth-gate';

export default function Page() {
  return (
    <AuthGate>
      <ResilientChat />
    </AuthGate>
  );
}
