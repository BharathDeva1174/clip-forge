import type { Metadata } from "next";

import JobWorkflow from "@/components/JobWorkflow";

export const metadata: Metadata = {
  title: "Video workflow",
};

/**
 * The workflow of one video is a blocking route.
 *
 * The job id is only known at request time, so the page cannot be
 * prerendered. That is the documented way to say so in Next 16: the
 * navigation into this route is allowed to wait for the id.
 */
export const instant = false;

/**
 * The workflow of one video.
 *
 * The route is a thin wrapper: the page is a client component because the
 * workflow follows a job that is still being worked on, and it has to keep
 * itself up to date.
 */
export default async function JobPage(props: PageProps<"/jobs/[jobId]">) {
  const { jobId } = await props.params;

  return (
    <div className="pt-10">
      <JobWorkflow jobId={jobId} />
    </div>
  );
}
