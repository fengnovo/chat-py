import type { Metadata } from 'next';
import type React from 'react';
import './globals.css';
import '@cognicatch/react/style.css';
import { AuthProvider } from './components/auth/auth-context';

export const metadata: Metadata = {
  title: 'Keen Agent',
  description: '多租户、可恢复、支持人工审批的 Headless Coding Agent 平台。',
  icons: {
    icon: [{ url: '/keen-ai-logo.png', type: 'image/png' }],
    shortcut: '/keen-ai-logo.png',
    apple: '/keen-ai-logo.png',
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang='zh-CN'
      className='h-full antialiased'
    >
      <body>
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
