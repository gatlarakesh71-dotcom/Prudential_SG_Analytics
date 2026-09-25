import express from 'express';
import { setRoutes } from './routes';
import { config } from './config';

const app = express();
const PORT = config.port || 3000;

// Middleware setup
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// Set up routes
setRoutes(app);

// Start the server
app.listen(PORT, () => {
    console.log(`Server is running on http://localhost:${PORT}`);
});