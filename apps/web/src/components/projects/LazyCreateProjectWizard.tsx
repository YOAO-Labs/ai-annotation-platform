import { lazy, Suspense, useEffect, useState, type ComponentProps } from "react";

const Wizard = lazy(() =>
  import("./CreateProjectWizard").then((module) => ({
    default: module.CreateProjectWizard,
  })),
);

export function CreateProjectWizard(props: ComponentProps<typeof Wizard>) {
  const [hasOpened, setHasOpened] = useState(props.open);
  useEffect(() => {
    if (props.open) setHasOpened(true);
  }, [props.open]);
  if (!props.open && !hasOpened) return null;
  return (
    <Suspense
      fallback={
        <p role="status" className="text-xs text-muted-foreground">
          正在加载项目向导…
        </p>
      }
    >
      <Wizard {...props} />
    </Suspense>
  );
}
