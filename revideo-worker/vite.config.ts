import {defineConfig} from 'vite';
import {createRequire} from 'module';

const require = createRequire(import.meta.url);
const revideo = require('@revideo/vite-plugin').default;

export default defineConfig({
  plugins: [
    revideo({
      project: ['./src/project.ts', './src/projects/*.ts'],
    }),
  ],
});
