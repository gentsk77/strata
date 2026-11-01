import "@fontsource-variable/newsreader"
import "noto-serif-sc/noto_serif_sc_regular/css.css"
import "@fontsource-variable/source-sans-3"
import "@fontsource-variable/source-code-pro"

import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import App from "./App.tsx"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
