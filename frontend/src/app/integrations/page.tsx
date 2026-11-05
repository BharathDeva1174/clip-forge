import IntegrationsPanel from "@/components/IntegrationsPanel";

export const metadata = {
  title: "Integrations",
};

/**
 * The AI provider configurations.
 *
 * The header, the navigation and the footer come from the layout, so this
 * page only decides what is in the middle.
 */
export default function IntegrationsPage() {
  return (
    <div className="py-10">
      <IntegrationsPanel />
    </div>
  );
}
