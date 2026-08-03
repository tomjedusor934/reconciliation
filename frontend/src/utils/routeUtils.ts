import { RouteRecordRaw } from 'vue-router';

/**
 * Extracts top-level routes from a router configuration.
 * Handles nested routes under AppShellView (extracted from children).
 * Filters out nested routes (e.g., /example/create, /example/:id).
 * Converts path to a readable label (e.g., /legal-texts -> Legal Texts).
 * 
 * @param routes The routes array from the router configuration
 * @returns An array of objects with value (path) and label
 */
export const getAppRoutes = (routes: Readonly<RouteRecordRaw[]>) => {
    const relevantRoutes: any[] = [];

    // Iterate through all routes
    for (const route of routes) {
        // If route has children (e.g., AppShellView with nested routes)
        if (route.children && route.children.length > 0) {
            for (const child of route.children) {
                const childPath = child.path || '';

                // Exclude create routes and dynamic parameter routes
                if (childPath.endsWith('/create')) continue;
                if (childPath.includes('/:')) continue;

                // Build full path
                let fullPath = '';
                if (route.path === '/') {
                    // Parent is root, child path is relative
                    fullPath = childPath === '' ? '/' : `/${childPath}`;
                } else {
                    // Parent is non-root, construct full path
                    fullPath = childPath === '' ? route.path : `${route.path}/${childPath}`;
                }

                relevantRoutes.push({
                    path: fullPath,
                    name: child.name || childPath
                });
            }
        } else {
            // No children, include if it's a relevant top-level route
            const path = route.path;

            // Exclude system routes and dynamic routes
            if (path === '/login' || path === '/:pathMatch(.*)*') continue;
            if (path.includes('/:')) continue;
            if (path.endsWith('/create')) continue;

            relevantRoutes.push(route);
        }
    }

    return relevantRoutes.map((route: any) => {
        let label = route.name ? String(route.name) : route.path;

        // Special case for root and home
        if (route.path === '/' || route.path === '') {
            label = 'home';
        } else {
            // Convert path to label: /legal-texts -> Legal Texts
            const raw = route.path.substring(1);
            label = raw.split(/[-_]/)
                .map(word => word.charAt(0).toUpperCase() + word.slice(1))
                .join(' ');
        }

        return {
            value: route.path,
            label: label
        };
    }).sort((a: any, b: any) => a.label.localeCompare(b.label)); // Alphabetical order
};
