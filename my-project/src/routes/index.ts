import { Router } from 'express';
import { someController } from '../controllers/someController';

const router = Router();

export const setRoutes = () => {
    router.get('/some-endpoint', someController.handleGetRequest);
    router.post('/another-endpoint', someController.handlePostRequest);
    // Add more routes as needed

    return router;
};