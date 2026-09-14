import ReactMarkdown from 'react-markdown'

/** Document content stays below page/card headings; HTML is never executed. */
export default function Markdown({ children }: { children: string }) {
  return <ReactMarkdown skipHtml components={{
    h1: ({ children }) => <h4>{children}</h4>,
    h2: ({ children }) => <h4>{children}</h4>,
    h3: ({ children }) => <h4>{children}</h4>,
  }}>{children}</ReactMarkdown>
}
