import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    // Cargo writes and locks build artifacts under src-tauri/target while
    // `tauri dev` compiles; watching them crashes Vite with EBUSY on Windows.
    watch: {
      ignored: ["**/src-tauri/**"]
    }
  }
});
