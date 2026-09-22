import { createContext, useCallback, useContext } from 'react'

/** Where the subsystem is mounted: '/maintenance' inside webuddy's AppShell,
 *  '/embed/maintenance' (or a host's own prefix) when embedded. Every in-subsystem
 *  link goes through this so an embedded page never navigates into the main shell. */
export const MaintenanceBaseContext = createContext('/maintenance')

export function useMaintenancePath() {
  const base = useContext(MaintenanceBaseContext)
  return useCallback((path = '') => `${base}${path}`, [base])
}
