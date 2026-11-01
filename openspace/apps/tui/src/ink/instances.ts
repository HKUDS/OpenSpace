export type InkInstanceControls = {
  invalidatePrevFrame: () => void;
  forceRedraw: () => void;
};

const instances = new Map<NodeJS.WriteStream, InkInstanceControls>();

export default instances;
