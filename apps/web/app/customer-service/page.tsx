import { AuthGate } from '@/app/components/auth/auth-gate';
import CustomerServiceChat from './customer-service-chat';

export const metadata = {
  title: 'AI 客服助手',
};

export default function CustomerServicePage() {
  return (
    <AuthGate>
      <CustomerServiceChat />
    </AuthGate>
  );
}
