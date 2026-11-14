import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { initMock } from "./api/client";
import App from "./hud/App";
import "./styles/base.css";
import "./styles/hud.css";

initMock("screen");

const root = document.getElementById("root");
if (!root) throw new Error("index.html has no #root");
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
