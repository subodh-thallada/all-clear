import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { initMock } from "./api/client";
import PanelPage from "./panel/PanelPage";
import "./styles/base.css";
import "./styles/panel.css";

initMock("panel");

const root = document.getElementById("root");
if (!root) throw new Error("panel.html has no #root");
createRoot(root).render(
  <StrictMode>
    <PanelPage />
  </StrictMode>,
);
