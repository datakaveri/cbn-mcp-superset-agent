import { logout } from '../auth/keycloak';
import { LOGO_URL } from '../config';
import { useAppConfig } from '../context';

export function Header() {
  const { username } = useAppConfig();
  return (
    <header className="app-header">
      <div className="brand">
        <img className="brand-logo" src={LOGO_URL} alt="CBN" />
        <div className="brand-text">
          <span className="brand-title">CBN Analytics</span>
          <span className="brand-sub">Ask. Visualize. Decide.</span>
        </div>
      </div>
      {username && (
        <div className="user-area">
          <span className="user-name" title={username}>
            {username}
          </span>
          <button className="logout-btn" onClick={() => logout()}>
            Sign out
          </button>
        </div>
      )}
    </header>
  );
}
