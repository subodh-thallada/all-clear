import { useEffect, useRef, useState } from 'react';
import * as Icon from './icons.jsx';

const SAYS = {
  missing: 'These buttons change the live run, so they need the admin key.',
  refused: 'The API refused the key this browser had saved.',
  wrong: 'That key was refused.',
  unreachable: "The API didn't answer. Check that it is running, then try again.",
};

// Asks for ADMIN_TOKEN when a live button has no working key. The button that
// was pressed runs as soon as the key is accepted.
export default function KeyPrompt({ s, actions }) {
  const [value, setValue] = useState('');
  const input = useRef(null);
  const checking = s.keyAsk === 'checking';
  useEffect(() => {
    input.current?.focus();
  }, [s.keyAsk]);
  const submit = (e) => {
    e.preventDefault();
    actions.setKey(value);
  };
  return (
    <form className="keyprompt" onSubmit={submit} onKeyDown={(e) => e.key === 'Escape' && actions.dismissKey()}>
      <p className={s.keyAsk === 'missing' || checking ? '' : 'bad'}>{SAYS[s.keyAsk] || SAYS.missing}</p>
      <p className="hint">Paste ADMIN_TOKEN from level-a/.env. This browser keeps it.</p>
      <div className="row">
        <input ref={input} type="password" autoComplete="off" spellCheck={false} placeholder="Admin key"
          value={value} onChange={(e) => setValue(e.target.value)} aria-label="Admin key" />
        <button type="submit" className="tb primary" disabled={checking || !value.trim()}>
          <Icon.Lock size={15} /> {checking ? 'Checking' : 'Unlock'}
        </button>
        <button type="button" className="tb" onClick={actions.dismissKey}>Cancel</button>
      </div>
    </form>
  );
}
