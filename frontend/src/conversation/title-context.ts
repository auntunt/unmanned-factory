import { createContext, useContext } from 'react'

/** Lets a run view publish the current work title up to the shell top bar. */
export const WorkTitleContext = createContext<(title: string | null) => void>(() => {})
export const useWorkTitle = () => useContext(WorkTitleContext)
