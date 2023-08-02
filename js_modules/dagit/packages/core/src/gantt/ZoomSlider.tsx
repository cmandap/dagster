import {Colors, SliderStyles} from '@dagster-io/ui';
import * as React from 'react';
import styled from 'styled-components';

/**
 * Renders a horizontal slider that lets you adjust the graph's relative zoom from 1-100.
 * It uses Blueprint CSS but not the Slider component, because that renders twice and
 * forces a DOM layout to determine it's size (I think for tick marks, which we aren't using)
 */
export const ZoomSlider: React.FC<{
  value: number;
  onChange: (v: number) => void;
}> = React.memo((props) => {
  return (
    <ZoomSliderContainer
      $fillColor={Colors.Gray600}
      className="bp4-slider bp4-slider-unlabeled"
      onMouseDown={(e: React.MouseEvent) => {
        const rect = e.currentTarget.closest('.bp4-slider')!.getBoundingClientRect();

        let initialX: number;
        if (e.target instanceof HTMLElement && e.target.classList.contains('bp4-slider-handle')) {
          initialX = e.pageX;
        } else {
          initialX = rect.left + (props.value / 100) * rect.width;
        }

        const onUpdate = (e: MouseEvent) => {
          const nextValue = props.value + (e.pageX - initialX) * (100 / rect.width);
          props.onChange(Math.max(0, Math.min(100, nextValue)));
        };
        const onRelease = (e: MouseEvent) => {
          onUpdate(e);
          document.removeEventListener('mousemove', onUpdate);
          document.removeEventListener('mouseup', onRelease);
        };
        document.addEventListener('mousemove', onUpdate);
        document.addEventListener('mouseup', onRelease);
      }}
    >
      <div className="bp4-slider-track">
        <div className="bp4-slider-progress" style={{left: 0, right: 0, top: 0}} />
        <div
          className="bp4-slider-progress bp4-intent-primary"
          style={{left: 0, right: `${100 - props.value}%`, top: 0}}
        />
      </div>
      <div className="bp4-slider-axis" />
      <span
        className="bp4-slider-handle"
        style={{left: `calc(${props.value}% - 8px)`}}
        tabIndex={0}
      />
    </ZoomSliderContainer>
  );
});

const ZoomSliderContainer = styled.div`
  ${SliderStyles}
`;
