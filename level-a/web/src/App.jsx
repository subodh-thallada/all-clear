import { useWall } from './data/source.js';
import { useHotkeys } from './ui/hooks.js';
import TopBar from './ui/TopBar.jsx';
import StoreList from './ui/StoreList.jsx';
import Flow from './ui/Flow.jsx';
import Banner from './ui/Banner.jsx';
import Toolbar from './ui/Toolbar.jsx';
import CallPanel from './ui/CallPanel.jsx';

export default function App() {
  const { state: s, actions } = useWall();

  useHotkeys({
    space: actions.playAll,
    1: actions.releaseStores,
    2: actions.armFailpoint,
    3: () => actions.killWorker(),
    4: actions.switchNorth,
    5: actions.saboteur,
    6: actions.storeEvent,
    7: actions.closeRecall,
    r: actions.reset,
    p: actions.togglePause,
  });

  return (
    <div className="app">
      <TopBar s={s} actions={actions} />
      <div className="body">
        <StoreList s={s} actions={actions} />
        <main className="stage">
          <Banner s={s} />
          <Flow s={s} actions={actions} />
          <Toolbar s={s} actions={actions} />
        </main>
        <CallPanel s={s} actions={actions} />
      </div>
    </div>
  );
}
